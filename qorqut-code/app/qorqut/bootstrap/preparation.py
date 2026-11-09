"""Generate CLI integration from one registered agent and pinned artifact set."""

from __future__ import annotations

from qorqut.inbox.defaults import legacy_inbox_skill
from qorqut.agents import role_workflow
from qorqut.agents import cleanup_workflow
from qorqut.agents.operational_delegation import supports_durable_delegation_version
from qorqut.agents.roles import GLOBAL_WORKER_ROLE, ORDUBASHY_ROLE, RETIRED_ROLE_IDS
from qorqut.composition.locations import agent_workspace, business_code, business_hq

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
import shlex
import sys
import subprocess
import tomllib
from typing import Any, Callable

import yaml
from qorqut_agentic.behavior import instructions as runtime_instructions

from qorqut.catalog.packages import content_hash, files_digest, relative_path
from qorqut_operations.contracts import DomainError, Principal, new_id, utc_now
from qorqut_operations.storage import safe_io
from .client import auth_headers, path_has_symlink, service_url
from . import email_guidance
from .tool_permissions import (VERSION as TOOL_CONFIGURATION_VERSION, fingerprint as tool_fingerprint,
                               permissions as tool_permissions, preparation_lock)

MINIMUM_CLAUDE_VERSION = (2, 1, 263)
MINIMUM_CODEX_VERSION = (0, 153, 4)
RUNTIMES = ("claude-code", "codex")
LEGACY_PLUGINS = {"biz-agents-manager@bazarlyk", "biz-agents-marketing@bazarlyk"}
# External MCP connections come only from installed extensions (resolver.native_connections); a registered role may
# declare one by name, and any name that no installed extension provides is refused (see external_connections). A
# previously prepared entry stays managed (see previous_connections) and is removed on the next authorized preparation.
INTEGRATION_DIRECTORY = ".qorqut/integrations"

def compatibility_report(version: str) -> dict:
    match = re.fullmatch(r"(?:Claude Code\s+)?(\d+)\.(\d+)\.(\d+)(?: \(Claude Code\))?", version.strip())
    parsed = tuple(map(int, match.groups())) if match else None
    supported = parsed is not None and parsed >= MINIMUM_CLAUDE_VERSION
    return {"supported": supported, "version": version, "minimum_version": ".".join(map(str, MINIMUM_CLAUDE_VERSION)),
            "required_features": ["http_mcp_headers_helper", "project_skills", "session_start_context", "prompt_guard", "reload_skills",
                                  "stop_hook_correction"],
            "verification": "version_and_generated_contract", "live_session_observed": False}


def runtime_compatibility(runtime: str, version: str) -> dict:
    if runtime == "claude-code":
        return compatibility_report(version)
    if runtime != "codex":
        raise DomainError("unsupported_runtime", "Unsupported agent CLI")
    match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)", version.strip())
    parsed = tuple(map(int, match.groups())) if match else None
    return {"supported": parsed is not None and parsed >= MINIMUM_CODEX_VERSION,
            "version": version, "minimum_version": ".".join(map(str, MINIMUM_CODEX_VERSION)),
            "required_features": ["http_mcp_headers_helper", "project_skills", "hooks", "session_queue"],
            "verification": "version_and_generated_contract", "live_session_observed": False}


def checkpoint_protocols(versions: dict, reports: dict) -> dict:
    """Per-client protocol 1 enablement, recorded in the local registration."""
    from .features import checkpoint_supported
    return {runtime: 1 if reports[runtime]["supported"] and checkpoint_supported(executable) else 0
            for runtime, executable in (("claude-code","claude"),("codex","codex"))}


def durable_delegation_enabled(data: dict, *, delegate_operations: bool) -> bool:
    """Gate parent-model pinning on the exact role artifact and explicit scope capability."""
    role = data.get("role") or {}
    scope = data.get("scope") or {}
    return (delegate_operations and supports_durable_delegation_version(role.get("version"), role.get("id"))
        and scope.get("kind") == "ecosystem"
        and "manage_delegated_assignments" in scope.get("global_capabilities", []))


def installed_versions() -> dict[str, str]:
    versions = {}
    for runtime, executable in (("claude-code", "claude"), ("codex", "codex")):
        try:
            versions[runtime] = subprocess.run([executable, "--version"], check=True,
                capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            versions[runtime] = ""
    return versions


def file_runtime(name: str) -> str | None:
    if name in {"CLAUDE.md", ".mcp.json"} or name.startswith(".claude/"):
        return "claude-code"
    if name in {"AGENTS.md", ".qorqut/codex-hook"} or name.startswith((".codex/", ".agents/")):
        return "codex"
    return None


def managed_skill_directories(workspace: Path, *, runtime=None):
    for directory in (".claude/skills", ".agents/skills"):
        if runtime is not None and file_runtime(directory + "/") != runtime:
            continue
        root = workspace / directory
        if root.exists():
            yield from sorted(path for path in root.iterdir()
                              if path.name.startswith("qorqut-") or path.name == role_workflow.SKILL_NAME)


def managed_skill_file(name: str) -> bool:
    return name == runtime_instructions.SNAPSHOT or name.startswith((".claude/skills/qorqut-", ".agents/skills/qorqut-",
                            *(directory + "/" for directory in role_workflow.SKILL_DIRECTORIES),
                            *(directory + "/" for directory in cleanup_workflow.SKILL_DIRECTORIES)))


def managed_integration_file(name: str) -> bool:
    return name.startswith(INTEGRATION_DIRECTORY + "/")


def external_connections(resolver, data) -> list[str]:
    """Only a registered role may declare an external connection, and only one an installed extension provides; an
    installed extension may also serve the general roles it names without the role package declaring it."""
    role = resolver.catalog.get("role", data["role"])
    declared = list(role.manifest.get("external_connections", []) or [])
    available = getattr(resolver, "native_connections", None) or {}
    unknown = sorted(set(declared) - set(available))
    if unknown:
        raise DomainError("invalid_configuration", "Unknown external connection: " + ", ".join(unknown))
    served = {name for name, connection in available.items() if role.manifest.get("id") in connection.roles}
    return sorted({*declared, *served})


def _contribution_path(name, owned) -> str:
    """A generated helper must stay under the managed integration directory with a plain relative path."""
    parts = name.split("/") if isinstance(name, str) else ()
    if (not name.startswith(INTEGRATION_DIRECTORY + "/") or name.endswith("/") or any(part in {"", ".", ".."} for part in parts)
            or "\0" in name or name in owned):
        raise DomainError("invalid_configuration", "An extension helper path must be unique and stay under the managed integration directory")
    return name


def _preserved_connections(workspace: Path, managed: set[str]):
    """Foreign MCP servers and tool-enable names survive re-preparation.

    Claude and Codex entries are preserved separately, verbatim in their own
    format; `existing` names every server found in either configuration.
    """
    servers, enabled, existing, codex_servers = {}, [], set(), {}
    try:
        document = json.loads(safe_io.read_bytes(workspace / ".mcp.json"))
    except (OSError, ValueError, DomainError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("mcpServers"), dict):
        existing = set(document["mcpServers"])
        servers = {name: entry for name, entry in document["mcpServers"].items() if name not in managed}
    try:
        settings = json.loads(safe_io.read_bytes(workspace / ".claude/settings.json"))
    except (OSError, ValueError, DomainError):
        settings = None
    if isinstance(settings, dict) and isinstance(settings.get("enabledMcpjsonServers"), list):
        enabled = [name for name in settings["enabledMcpjsonServers"] if isinstance(name, str) and name not in managed]
    try:
        codex = tomllib.loads(safe_io.read_bytes(workspace / ".codex/config.toml").decode())
    except (OSError, ValueError, DomainError):
        codex = None
    if isinstance(codex, dict) and isinstance(codex.get("mcp_servers"), dict):
        existing |= set(codex["mcp_servers"])
        codex_servers = {name: entry for name, entry in codex["mcp_servers"].items() if name not in managed}
    return servers, enabled, existing, codex_servers


def _bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def _absolute_rule(path: Path) -> str:
    return "/" + str(path)


@dataclass(frozen=True)
class Projection:
    files: dict[str, bytes]
    metadata: dict


def _coordinator_workspace(resolver) -> Path | None:
    """The configured Ordubashy's registered workspace, the source of a new worker's inherited model settings."""
    with resolver.database.read() as uow:
        installation = uow.repository("installation").get("service") or {}
        row = uow.connection.execute("SELECT workspace FROM agent_profiles WHERE agent_id=?",
                                     (installation.get("management_coordinator_id"),)).fetchone()
    return Path(row[0]) if row else None


def _business_code_root(resolver, data, business) -> Path | None:
    """Validate a business code tree; every global may carry out explicitly assigned code work."""
    if data["scope"]["kind"] != "ecosystem":
        return None
    code = business_code(resolver.paths, business)
    if path_has_symlink(code) or (code.exists() and not code.is_dir()):
        raise DomainError("preparation_error", "Project code directory must be a local directory inside its registered workspace")
    return code


def skill_projection(resolver, data) -> tuple[dict, dict]:
    """Managed skill files for both CLIs and each selected skill's materialization hash."""
    view = resolver.require_agentic_view()

    def read():
        metadata, bodies = resolver.managed_skills(data, selection=data["agentic_selection"])
        files = {directory + name + "/" + relative_path(path): body
                 for name, resources in bodies.items() for path, body in resources.items()
                 for directory in (".claude/skills/", ".agents/skills/")}
        selected = {item["name"]: item["materialization_hash"] for item in metadata}
        if selected != {item["name"]: item["materialization_hash"] for item in data["skills"]}:
            raise DomainError("agentic_revision_conflict", "Managed skills changed after configuration resolution", retryable=True)
        files[runtime_instructions.SNAPSHOT] = view.runtime_instructions().bytes()
        return files, selected

    files, selected = view.consistent(read, revision=data["agentic_catalog_revision"])
    # The core-generated Qorqbot guide travels with the managed skills, so full preparation and skill refresh both keep it.
    from . import qorqbot_guide
    return {**files, **qorqbot_guide.skill_files()}, selected


def core_generated_skill(name: str) -> bool:
    """A skill folder Qorqut writes itself, outside the agent's managed skill selection (bootstrap/qorqbot_guide.py)."""
    from . import qorqbot_guide
    return name == qorqbot_guide.SKILL_NAME


def seed_instance_guidance(resolver, data) -> dict:
    """Validate the imported canonical mail guide; preparation never seeds a second copy."""
    validated = {}
    view = resolver.require_agentic_view()
    for grant in data["plugins"]:
        if grant["id"] == email_guidance.PLUGIN_ID:
            root = email_guidance.home(resolver.paths, agentic_view=view)
            prefix = "plugins/imported-email-management/guidance/email/"
            for name in sorted(email_guidance.GUIDE_FILES):
                resource = view.layer.catalog.read_resource("imported-email-management", "guidance/email/" + name,
                                                            selection=data["agentic_selection"])
                if resource["encoding"] != "utf-8" or resource["root_path"] != prefix + name:
                    raise DomainError("agentic_resource_unavailable", "Imported email guide is invalid")
            validated[grant["id"]] = {"path": str(root), "seeded": False, "source": "agentic"}
    resolver.check_agentic_revision(data["agentic_catalog_revision"])
    return validated


def _cli_wrappers(launch_settings: str, consent_marker: str, workspace: str, trust_command: str = ":") -> str:
    """The claude and codex wrappers, valid in both bash and zsh. Every argument is already shell-quoted.

    Qorqbot's options, trust refresh and role prompt apply only when the shell is physically in this agent's
    workspace root (``workspace``, an absolute path): the same folder as ``pwd -P`` reports, compared exactly. From any other
    folder, a subfolder of the workspace included, ``claude`` and ``codex`` run unchanged with the owner's own arguments, and
    so they do when the current folder cannot be determined.

    ``trust_command`` re-records folder trust for this workspace before each launch while the owner's consent exists
    (``python -m qorqut.agent_trust ensure``): another program that rewrites ``~/.claude.json`` from an older copy, such
    as another running Claude Code session, can drop the entry between the consent and the owner typing claude."""
    return (
        'unalias claude codex 2>/dev/null || true\n'
        # The trailing x keeps a path's own trailing newlines from being dropped by the command substitution. ``builtin``
        # skips the owner's own cd and pwd, and the cd output (a zsh chpwd hook or AUTO_PUSHD can print) is discarded.
        '_qorqbot_in_agent_root() { local here root; case ' + workspace + ' in /*) ;; *) return 1 ;; esac; '
        'here=$(builtin pwd -P 2>/dev/null && printf x) || return 1; '
        'root=$(builtin cd -P -- ' + workspace + ' >/dev/null 2>&1 && builtin pwd -P && printf x) || return 1; '
        '[ "$here" = "$root" ]; }\n'
        '_qorqbot_prelaunch() { if [ -f ' + consent_marker + ' ]; then ' + trust_command + ' >/dev/null 2>&1 || true; fi; }\n'
        # A bare claude or codex, as the owner types it in the agent's terminal, opens with the role prompt Start would
        # type: the agent takes up its work, and Codex writes the session file Chat needs (it does so only on a first turn).
        # Start itself types the prompt after launch and sets QORQBOT_ROLE_SENT so it is not sent twice.
        'claude() { if ! _qorqbot_in_agent_root; then command claude "$@"; return $?; fi; _qorqbot_prelaunch; '
        'if [ $# -eq 0 ] && [ -z "${QORQBOT_ROLE_SENT:-}" ]; then set -- /role; fi; '
        'command claude --settings ' + launch_settings + ' "$@"; }\n'
        'codex() { if ! _qorqbot_in_agent_root; then command codex "$@"; return $?; fi; _qorqbot_prelaunch; '
        'if [ $# -eq 0 ] && [ -z "${QORQBOT_ROLE_SENT:-}" ]; then set -- \'$role\'; fi; '
        'if [ -f ' + consent_marker + ' ]; then '
        'command codex --dangerously-bypass-hook-trust "$@"; '
        'else command codex "$@"; fi; }\n'
    )


def agent_shell(launch_settings: str, consent_marker: str, workspace: str, trust_command: str = ":") -> str:
    """The prepared bash agent shell (``bash --rcfile``)."""
    return ('# Generated Qorqut agent shell; keep personal shell setup.\n'
            'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi\n'
            + _cli_wrappers(launch_settings, consent_marker, workspace, trust_command))


# zsh (the macOS default) has no --rcfile: an agent terminal starts with ZDOTDIR at .qorqut/zsh (transport/terminals.py).
# These files load the owner's own .zshenv and .zshrc from their usual folder first, then add the wrappers, and leave
# ZDOTDIR as the owner had it so shells started from the agent terminal behave normally.
ZSH_ENV = (
    '# Generated Qorqut agent shell for zsh; loads your own .zshenv.\n'
    '_qorqbot_zdotdir="$ZDOTDIR"\n'
    'ZDOTDIR="${QORQBOT_USER_ZDOTDIR:-$HOME}"\n'
    'if [ -f "$ZDOTDIR/.zshenv" ]; then . "$ZDOTDIR/.zshenv"; fi\n'
    'QORQBOT_USER_ZDOTDIR="$ZDOTDIR"\n'
    'ZDOTDIR="$_qorqbot_zdotdir"\n'
    'unset _qorqbot_zdotdir\n'
)


def agent_zshrc(launch_settings: str, consent_marker: str, workspace: str, trust_command: str = ":") -> str:
    return ('# Generated Qorqut agent shell for zsh; loads your own .zshrc.\n'
            'ZDOTDIR="${QORQBOT_USER_ZDOTDIR:-$HOME}"\n'
            'if [ -f "$ZDOTDIR/.zshrc" ]; then . "$ZDOTDIR/.zshrc"; fi\n'
            'if [ "$ZDOTDIR" = "$HOME" ]; then unset ZDOTDIR; fi\n'
            'unset QORQBOT_USER_ZDOTDIR\n'
            + _cli_wrappers(launch_settings, consent_marker, workspace, trust_command))


# What an agent tells the owner about starting it. Its own "/role" instruction is about its work loop, and in a real
# run Ordubashy passed it on to the owner as the way to start him.
OWNER_START = ("/role and $role are for you, not the owner. The owner starts you from the Qorqbot dashboard: your own\n"
               "terminal there (the >_ button above your Chat), typing claude or codex, or the Start button in Chat. When the\n"
               "owner asks how to use Qorqbot, answer from the qorqut-qorqbot-guide skill; never send them to another\n"
               "terminal window, to cd into this folder or to type /role.\n")



def build_projection(resolver, config, *, registration_id: str, endpoint: str, interpreter: str,
                     capture_transcripts: bool = True, checkpoint_protocol: dict | None = None,
                     interpreter_validator: Callable[[str], bool] | None = None,
                     codex_permission_mode: str | None = None,
                     previous_connections=()) -> Projection:
    if type(capture_transcripts) is not bool:
        raise DomainError("invalid_configuration", "Transcript capture selection must be a boolean")
    data = config.to_dict()
    if codex_permission_mode is None:
        codex_permission_mode = "bypass" if data["scope"]["kind"] == "ecosystem" else "scoped"
    workspace = Path(data["workspace"])
    registration_path = workspace / ".qorqut/registration.json"
    executable = str(Path(interpreter).absolute())
    from qorqut.installation import helper_launcher
    stable_helper = helper_launcher()
    def helper(module, *arguments):
        return [stable_helper, module, *arguments] if stable_helper else [executable, '-I', '-m', module, *arguments]
    if interpreter_validator is not None and not callable(interpreter_validator):
        raise DomainError("invalid_configuration", "Interpreter validation callback is invalid")
    available = interpreter_validator(executable) if interpreter_validator is not None else Path(executable).is_file()
    if not isinstance(available, bool) or not available:
        raise DomainError("preparation_error", "The installed Python interpreter is unavailable")
    command = shlex.join(helper('qorqut.bootstrap.claude_session', '--registration', str(registration_path)))
    header_command = shlex.join(helper('qorqut.bootstrap.mcp_headers', '--registration', str(registration_path)))
    mcp_command = ['/bin/sh', str(workspace / '.qorqut/mcp-bridge')]
    mcp_launcher = '#!/bin/sh\nexec ' + shlex.join(helper('qorqut.bootstrap.mcp_reconnect', '--registration', str(registration_path))) + '\n'
    endpoint = service_url(endpoint)
    native = getattr(resolver, "native_connections", None) or {}
    connection_status, extension_connections = {}, {}
    connections = []
    for name in external_connections(resolver, data):
        if name in native:
            # The extension names its managed server; its readiness never reads a secret.
            extension_connections[native[name].server] = native[name]
            connection_status[native[name].server] = native[name].status(resolver.paths, data["agent_id"])
            connections.append(native[name].server)
        else:
            connections.append(name)
    prepared = [name for name in connections if connection_status.get(name, {"prepared": True})["prepared"]]
    # Previously prepared names stay managed so a removed or unavailable
    # connection is cleared; a declared but unprepared name stays foreign.
    previous = set(previous_connections)
    managed_names = {"qorqut", "bazarlyk", *prepared, *previous}
    for connection in extension_connections.values():
        managed_names.update(connection.retires)  # Retire an older prepared server on refresh.
    preserved_servers, preserved_enabled, existing_servers, preserved_codex = _preserved_connections(workspace, managed_names)
    for server, connection in extension_connections.items():
        if connection.require_new_binding and server in prepared and server not in previous and server in existing_servers:
            raise DomainError("projection_conflict", "An existing unmanaged connection requires reconciliation", details={"servers": [server]})
    # Claude and Codex use separate configuration shapes for the same server.
    claude_servers, codex_servers = {}, {}
    extension_files = {}
    for server, connection in extension_connections.items():
        if server in prepared:
            claude_servers[server] = dict(connection.entry(resolver.paths, workspace, data["agent_id"]))
            codex_servers[server] = dict(claude_servers[server])
            if connection.tools:
                codex_servers[server]["enabled_tools"] = list(connection.tools)
            codex_servers[server].pop("type", None)
            claude_servers[server].pop("required", None)
            for helper_name, body in dict(connection.files(resolver.paths, workspace, data["agent_id"])).items():
                extension_files[_contribution_path(helper_name, extension_files)] = bytes(body)
    defaults = {"enableAllProjectMcpServers": False,
        "enabledMcpjsonServers": ["qorqut", *prepared, *preserved_enabled],
        "permissions": tool_permissions(data["tools"])}
    protected = [workspace / "agent.yaml", workspace / "CLAUDE.md", workspace / "AGENTS.md", workspace / ".codex", workspace / ".agents", workspace / ".mcp.json", workspace / ".claude", workspace / ".qorqut", workspace / "PLAN.md", workspace / "notes",
                 resolver.paths.runtime, resolver.paths.context, resolver.paths.operations / "CLAUDE.md"]
    read_roots = []
    code_roots = []
    ordubashy = data["role"]["id"] == "ordubashy" and data["scope"]["kind"] == "ecosystem"
    delegate_operations = ordubashy and any(
        skill["name"] == "qorqut-role-operations-delegation"
        and skill.get("owner") == {"kind": "role", "id": data["role"]["id"]}
        for skill in data["skills"])
    durable_delegation = durable_delegation_enabled(data, delegate_operations=delegate_operations)
    with resolver.database.read() as uow:
        # Always-loaded skills are injected only for the configured Ordubashy.
        from qorqut.management.identity import configured_ordubashy
        always_skills = ordubashy and configured_ordubashy(uow) == data["agent_id"]
        # Every active business in the agent's derived scope; no home or membership is assumed.
        businesses = [row for row in uow.connection.execute(
            "SELECT id,slug FROM businesses WHERE archived_at IS NULL ORDER BY id")
            if row["id"] in data["scope"].get("allowed_business_ids", ())]
        for business in businesses:
            code_root = _business_code_root(resolver, data, business)
            if code_root is not None:
                code_roots.append(code_root)
            hq = business_hq(resolver.paths, business)
            protected += [hq / part for part in ("business.yaml", "CLAUDE.md", "context", "tasks", "log")]
            protected += [agent_workspace(resolver.paths, row[0], business) for row in uow.connection.execute("SELECT agent_id FROM agent_profiles WHERE home_business_id=? AND agent_id<>?", (business["id"], data["agent_id"]))]
            read_roots += [hq / part for part in ("context", "analytics", "assets")]
    local = {"hooks": {
        "SessionStart": [{"matcher": "startup|resume|clear|compact|fork", "hooks": [{"type": "command", "command": command, "timeout": 240}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": command, "timeout": 240}]}]},
        "permissions": {"allow": [f"Edit({_absolute_rule(workspace / 'work')}/**)", *[f"Read({_absolute_rule(path)}/**)" for path in read_roots]],
                        "deny": [f"Edit({_absolute_rule(path)}{('' if path.name.endswith(('.md', '.yaml', '.json')) else '/**')})" for path in protected]
                            + [f"Read({_absolute_rule(resolver.paths.runtime)}/**)", f"Read({_absolute_rule(workspace / '.qorqut')}/credential)"]}}
    if code_roots:
        local["permissions"]["allow"] += [f"Edit({_absolute_rule(path)}/**)" for path in code_roots]
        local["permissions"]["additionalDirectories"] = [str(path) for path in code_roots]
    # No runtime approval prompts. Service authorization remains authoritative.
    local["permissions"]["defaultMode"] = "dontAsk"
    local["permissions"]["allow"] += ["Read", "Glob", "Grep", "WebFetch", "WebSearch"]
    for server in prepared:
        connection = extension_connections.get(server)
        tools = connection.tools if connection and connection.tools else ("*",)
        local["permissions"]["allow"] += ["mcp__" + server + "__" + tool for tool in tools]
    if data["scope"]["kind"] == "ecosystem":
        # Claude refuses project-local bypass defaults. The prepared terminal
        # passes a trusted --settings file; local dontAsk also prevents popups
        # when starting outside the prepared terminal.
        local["permissions"]["allow"] += ["Bash", "Edit", "Write", "Agent"]
        local["permissions"]["deny"] = []
    local["permissions"]["deny"] = [*local["permissions"]["deny"], *connector_gate_guard(workspace)]
    if capture_transcripts:
        snapshot_command = shlex.join(helper('qorqut.bootstrap.transcript_snapshot', '--registration', str(registration_path)))
        for event in ("Stop", "SessionEnd"):
            local["hooks"][event] = [{"hooks": [{"type": "command", "command": snapshot_command, "timeout": 3}]}]
    # Claude bounds initial stdio startup separately from per-server tool execution.
    # This setting is agent-folder-local, never a personal/global CLI setting.
    local.setdefault('env', {})['MCP_TIMEOUT'] = '240000'
    from .codex_config import add_activity_hooks, configuration, role_model_default, worker_model_default
    model_default = role_model_default(data["scope"]["kind"], data["role"]["id"])
    if model_default is not None and data["role"]["id"] == GLOBAL_WORKER_ROLE:
        model_default = worker_model_default(workspace, _coordinator_workspace(resolver), model_default)
    # Codex trusts the exact hook definition. Keep release-specific interpreter
    # paths inside the protected prepared helper, not in the hook command: a
    # routine deployment must not invalidate every previously reviewed hook.
    codex_helper = workspace / '.qorqut/codex-hook'
    codex_entry = shlex.join(['/bin/sh', str(codex_helper)])
    codex_command = codex_entry + ' bootstrap'
    codex_lifecycle = codex_entry + ' lifecycle'
    codex_config, codex_hooks = configuration(workspace=workspace, endpoint=endpoint,
        header_command=header_command, command=codex_command, snapshot_command=codex_lifecycle, mcp_command=mcp_command,
        protected=protected, read_denied=[resolver.paths.runtime, workspace / ".qorqut/credential"],
        write_roots=code_roots, delegate_operations=delegate_operations,
        external_servers=codex_servers, preserved_servers=preserved_codex, permission_mode=codex_permission_mode or "scoped",
        model_default=model_default)
    # A standalone stdlib hook avoids loading the full application on every action.
    activity_script = str(workspace / '.qorqut/activity-hook.py')
    for runtime, hooks in (('claude-code',local['hooks']),('codex',codex_hooks)):
        activity_command = shlex.join([executable,'-I',activity_script,'--registration',str(registration_path),'--runtime',runtime])
        if stable_helper:
            activity_command = shlex.join(helper('qorqut.bootstrap.activity_hook', '--registration', str(registration_path), '--runtime', runtime))
        if runtime == 'codex':
            activity_command = codex_entry + ' activity'
        add_activity_hooks(hooks, activity_command, runtime)
        if runtime == 'claude-code':
            # Longer than the gate's own request timeout: Claude Code lets a call continue when a hook times out.
            hooks['PreToolUse'].append({'matcher': 'mcp__claude_ai_.*', 'hooks': [
                {'type': 'command', 'command': activity_command + ' --connector-gate', 'timeout': 30}]})
    skills_script = str(workspace / '.qorqut/always-skills.py')
    # The main hook composes skills and runtime rules before contextual data.
    profile = {key: data[key] for key in ("schema_version", "agent_id", "profile_revision", "name", "display_name", "status", "runtime", "role", "scope", "assignment_brief", "plugins", "tool_policy", "presentation", "created_at")}
    profile["generated"] = True
    from qorqut.agentic.text import render
    from qorqut_agentic.behavior.workspace import documents
    view = resolver.require_agentic_view()
    software_boundary = render(view, "host", "app/qorqut/agentic/assets/software-ownership.md")
    if not software_boundary.strip():
        raise DomainError("agentic_resource_unavailable", "Managed software ownership guidance is empty")
    instruction_documents = documents(view.runtime_instructions(),
        display_name=data["display_name"], agent_id=data["agent_id"], software_boundary=software_boundary,
        extension_orientations=[connection.orientation(server in prepared, ordubashy, agentic_view=view)
                               for server, connection in extension_connections.items()],
        ordubashy=ordubashy, code_roots=code_roots, delegate_operations=delegate_operations,
        inbox_available=any(tool['canonical_operation'] == 'inbox_list' and tool['available'] for tool in data['tools']))
    codex_targets = {
        'bootstrap': shlex.join([executable, '-I', '-m', 'qorqut.bootstrap.claude_session',
                                '--registration', str(registration_path), '--runtime', 'codex']),
        'lifecycle': shlex.join([executable, '-I', '-m', 'qorqut.bootstrap.codex_session',
                               '--registration', str(registration_path)]),
        'activity': shlex.join([executable, '-I', activity_script, '--registration', str(registration_path),
                              '--runtime', 'codex']),
    }
    if stable_helper:
        codex_targets = {mode: shlex.join(helper(module, '--registration', str(registration_path), *extra))
            for mode, module, extra in (
                ('bootstrap', 'qorqut.bootstrap.claude_session', ('--runtime', 'codex')),
                ('lifecycle', 'qorqut.bootstrap.codex_session', ()),
                ('activity', 'qorqut.bootstrap.activity_hook', ('--runtime', 'codex')))}
    if always_skills:
        codex_targets['skills'] = shlex.join([executable, '-I', skills_script, '--registration', str(registration_path),
                                              '--runtime', 'codex'])
    codex_launcher = '#!/bin/sh\n# Generated by trusted agent preparation. No model or watcher starts during preparation.\n'
    codex_launcher += '[ "$#" -eq 1 ] || exit 64\ncase "$1" in\n'
    codex_launcher += ''.join(f'  {mode}) exec {target} ;;\n' for mode, target in codex_targets.items())
    codex_launcher += '  *) exit 64 ;;\nesac\n'
    registration_document = {"durable_delegation_enabled": durable_delegation, "operational_intake_enabled": always_skills}
    # The stop hook's dirty-turn watermark ignores these exposed read tools.
    kinds = {entry.name: entry.effect_kind for entry in resolver.registry.entries()}
    read_operations = sorted({name for tool in data["tools"] if tool["available"]
        and kinds.get(tool["canonical_operation"]) == "read"
        for name in (tool["canonical_operation"], *tool["exposed_aliases"])})
    files = {
        runtime_instructions.HELPER: Path(runtime_instructions.__file__).read_bytes(),
        '.qorqut/mcp-bridge': mcp_launcher.encode(),
        '.qorqut/codex-hook': codex_launcher.encode(),
        **({'.qorqut/always-skills.py': Path(__file__).with_name('always_skills.py').read_bytes()} if always_skills else {}),
        '.qorqut/activity-hook.py': Path(__file__).with_name('activity_hook.py').read_bytes(),
        # The activity hook runs with Python isolation and therefore needs its
        # stdlib-only outbox helper beside the generated script.
        '.qorqut/technical_reporting.py': Path(__file__).with_name('technical_reporting.py').read_bytes(),
        '.qorqut/agentic': ('#!/bin/sh\nexec ' + shlex.join(helper(
            'qorqut.agentic.client', '--registration', str(registration_path))) + '\n').encode(),
        "agent.yaml": yaml.safe_dump(profile, sort_keys=False, allow_unicode=False).encode(),
        "CLAUDE.md": instruction_documents['CLAUDE.md'],
        "AGENTS.md": instruction_documents['AGENTS.md'],
        ".codex/config.toml": codex_config,
        ".codex/hooks.json": _bytes({"hooks": codex_hooks}),
        ".mcp.json": _bytes({"mcpServers": {**preserved_servers,
            "qorqut": {"type": "stdio", "command": mcp_command[0], "args": mcp_command[1:], "timeout": 240000},
            **claude_servers}}),
        ".claude/settings.json": _bytes(defaults), ".claude/settings.local.json": _bytes(local),
        ".qorqut/registration.json": _bytes({**registration_document, "schema_version": 1,
            "registration_id": registration_id, "agent_id": data["agent_id"], "workspace": str(workspace),
            "service_url": endpoint, "credential_file": ".qorqut/credential",
            "capture_transcripts": capture_transcripts,
            # Focus curators use the same verified native continuation contract,
            # with Focus-only review (2), not the broader work/inbox checkpoint (1).
            "checkpoint_protocol": {runtime:(2 if enabled and ordubashy else enabled)
                                    for runtime,enabled in (checkpoint_protocol or {}).items()},
            "read_operations": read_operations}),
    }
    claude_mode = "bypassPermissions" if data["scope"]["kind"] == "ecosystem" else "dontAsk"
    files[".qorqut/claude-launch-settings.json"] = _bytes({"permissions": {"defaultMode": claude_mode}})
    launch_settings = shlex.quote(str(workspace / ".qorqut/claude-launch-settings.json"))
    from qorqut.agent_trust import marker_path
    # Codex runs Qorqbot's prepared hooks without a separate review only while the owner's consent file exists
    # (qorqut.agent_trust); removing it restores Codex's own hook review for the next launch.
    consent_marker = shlex.quote(str(marker_path(resolver.paths.runtime)))
    trust_command = " ".join(shlex.quote(part) for part in (
        executable, "-I", "-m", "qorqut.agent_trust", "ensure", "--runtime", str(resolver.paths.runtime), "--folder", str(workspace)))
    workspace_root = shlex.quote(str(workspace))
    files[".qorqut/agent-shell.bash"] = agent_shell(launch_settings, consent_marker, workspace_root, trust_command).encode()
    files[".qorqut/zsh/.zshenv"] = ZSH_ENV.encode()
    files[".qorqut/zsh/.zshrc"] = agent_zshrc(launch_settings, consent_marker, workspace_root, trust_command).encode()
    for name in extension_files:
        if name in files:
            raise DomainError("invalid_configuration", "An extension helper duplicates a generated file")
    files.update(extension_files)
    for server, connection in extension_connections.items():
        if server in prepared:
            guidance = connection.guidance(agentic_view=resolver.require_agentic_view()).encode()
            for name in ("CLAUDE.md", "AGENTS.md"):
                files[name] += guidance
    skill_files, selected = skill_projection(resolver, data)
    files.update(skill_files)
    from .browser import SKILL_NAME
    if data["role"]["id"] == ORDUBASHY_ROLE and SKILL_NAME in selected:
        # A native command, discovered only inside the selected skill. Runtime
        # target configuration is operator-owned and survives re-preparation.
        script = ("from qorqut.bootstrap.browser import main; raise SystemExit(main(" + repr(str(workspace)) + "))")
        files[".qorqut/browser"] = ("#!/bin/sh\nexec " + shlex.join([executable, "-I", "-c", script]) + ' "$@"\n').encode()
        if stable_helper:
            files['.qorqut/browser'] = ('#!/bin/sh\nexec ' + shlex.join(helper(
                'qorqut.bootstrap.browser_entry', '--workspace', str(workspace))) + ' "$@"\n').encode()
    metadata = {"schema_version": 1, "helper_runtime":{"executable":stable_helper or executable,"stable":bool(stable_helper)}, "codex_permission_mode": codex_permission_mode or "scoped",
                "native_guidance_digest": data["native_guidance_digest"],
                "business_code_roots": [str(path) for path in code_roots],
                "files": {name: content_hash(body) for name, body in sorted(files.items())},
                "selected_skills": selected,
                "external_connections": connections,
                "prepared_connections": prepared,
                "connection_status": connection_status,
                "artifact_refs": {"role": data["role"], "plugins": [{key: item[key] for key in ("id", "version", "digest")} for item in data["plugins"]]},
                "settings_digest": files_digest({name: body for name, body in files.items() if name in {".mcp.json", ".claude/settings.json", ".claude/settings.local.json"}}),
                "skills_digest": files_digest({name: body for name, body in files.items() if name.startswith(".claude/skills/")}),
                "tool_configuration_version": TOOL_CONFIGURATION_VERSION,
                "tool_configuration": tool_fingerprint(data["tools"])}
    if resolver.native_guidance_digest(data) != data["native_guidance_digest"]:
        raise DomainError("agentic_revision_conflict", "Managed native guidance changed during projection", retryable=True)
    resolver.check_agentic_revision(data["agentic_catalog_revision"])
    return Projection(files, metadata)


def inspect_projection(workspace: Path, projection: dict, *, runtime="claude-code") -> list[str]:
    drift = []
    expected = projection.get("files", {})
    if runtime == "claude-code":
        legacy_role = workspace / ".claude/commands/role.md"
        if path_has_symlink(legacy_role) or legacy_role.exists():
            drift.append(".claude/commands/role.md")
    for name, digest in expected.items():
        if file_runtime(name) not in (None, runtime):
            continue
        path = workspace / relative_path(name)
        if (path_has_symlink(path) or not path.is_file() or content_hash(safe_io.read_bytes(path)) != digest
                or name == ".qorqut/agentic" and not path.stat().st_mode & 0o100):
            drift.append(name)
    skill_directory = ".agents/skills" if runtime == "codex" else ".claude/skills"
    skills = workspace / skill_directory
    if path_has_symlink(skills):
        drift.append(skill_directory)
    elif skills.exists():
        for path in managed_skill_directories(workspace, runtime=runtime):
            if path.is_symlink() or not path.is_dir():
                drift.append(str(path.relative_to(workspace)))
                continue
            for child in sorted(path.rglob("*")):
                if (child.is_file() or child.is_symlink()) and child.relative_to(workspace).as_posix() not in expected:
                    drift.append(child.relative_to(workspace).as_posix())
            if path.name not in projection.get("selected_skills", {}) and not core_generated_skill(path.name):
                drift.append(path.relative_to(workspace).as_posix())
    for name in ((".claude/settings.json", ".claude/settings.local.json") if runtime == "claude-code" else ()):
        if path_has_symlink(workspace / name):
            continue
        try:
            settings = json.loads(safe_io.read_bytes(workspace / name))
        except (ValueError, OSError):
            continue
        if any(settings.get("enabledPlugins", {}).get(plugin) for plugin in LEGACY_PLUGINS):
            drift.append("legacy_role_plugin_enabled")
    return sorted(set(drift))


def connector_gate_guard(workspace: Path) -> list[str]:
    """Keep an agent from editing away its own approval hook. Every terminal open also rewrites these files."""
    files = (workspace / ".claude/settings.json", workspace / ".claude/settings.local.json", workspace / ".qorqut/activity-hook.py")
    return [*(f"Edit({_absolute_rule(path)})" for path in files), "Bash(*.claude/settings*)", "Bash(*.qorqut/activity-hook*)"]


def _write_file(path: Path, data: bytes, *, private=False, executable=False) -> None:
    if path_has_symlink(path):
        raise DomainError("projection_conflict", "Generated projection cannot follow symlinks")
    safe_io.atomic_write(path, data, mode=0o700 if executable else 0o600 if private else 0o644,
                         enforce_mode=private or executable)


def prepare_agent(resolver, principal: Principal, *, endpoint: str, versions: dict | None = None,
                         interpreter: str | None = None, migrate_legacy: bool = False,
                         capture_transcripts: bool = True,
                         interpreter_validator: Callable[[str], bool] | None = None,
                         codex_permission_mode: str | None = None) -> dict:
    """Trusted backend preparation; call only for an already approved profile."""
    from qorqut_operations.storage.admission import InstallationRecoveryLock
    with preparation_lock(resolver.paths, principal.id), InstallationRecoveryLock(resolver.locks.recovery).writer():
        return _prepare_agent_locked(resolver, principal, endpoint=endpoint, versions=versions, interpreter=interpreter,
            migrate_legacy=migrate_legacy, capture_transcripts=capture_transcripts, interpreter_validator=interpreter_validator,
            codex_permission_mode=codex_permission_mode)


def _prepare_agent_locked(resolver, principal: Principal, *, endpoint: str, versions: dict | None = None,
                         interpreter: str | None = None, migrate_legacy: bool = False,
                         capture_transcripts: bool = True,
                         interpreter_validator: Callable[[str], bool] | None = None,
                         codex_permission_mode: str | None = None) -> dict:
    from qorqut_operations.application import Application
    # Release code may live outside the service's virtual environment. Preserve
    # the interpreter path (including its venv symlink), not its resolved binary.
    interpreter = interpreter if interpreter is not None else sys.executable
    versions = installed_versions() if versions is None else versions
    reports = {runtime: runtime_compatibility(runtime, versions.get(runtime, "")) for runtime in RUNTIMES}
    compatibility = {"supported": any(report["supported"] for report in reports.values()),
                     "runtimes": reports}
    if not compatibility["supported"]:
        raise DomainError("unsupported_runtime", "No installed agent CLI meets the supported preparation baseline", details=compatibility)
    config = resolver.resolve_agent_config(principal)
    data = config.to_dict()
    if data["role"]["id"] in RETIRED_ROLE_IDS:
        raise DomainError("retired_role", "This role is retired; its profiles may be read or deactivated but not prepared")
    workspace = Path(data["workspace"])
    if any(path_has_symlink(workspace / path) for path in (".claude", ".claude/skills", ".codex", ".agents", ".agents/skills", ".qorqut", "notes", "work")):
        raise DomainError("projection_conflict", "Prepared project directories cannot follow symlinks")
    with resolver.database.read() as uow:
        Application.ensure_writes_open(uow)
        existing = uow.repository("machine_registrations").list(filters={"workspace": str(workspace)}, limit=2)
    registration = existing[0] if existing else None
    if registration and registration["principal_id"] != principal.id:
        raise DomainError("registration_conflict", "Prepared folder is already bound to a different principal")
    old = registration["projection_json"] if registration else {}
    if codex_permission_mode is None:
        codex_permission_mode = old.get("codex_permission_mode",
            "bypass" if data["scope"]["kind"] == "ecosystem" else "scoped")
    if codex_permission_mode not in {"scoped", "bypass"}:
        raise DomainError("validation_error", "Codex permission mode must be scoped or bypass")
    unmanaged = []
    for path in managed_skill_directories(workspace):
        if path.name not in old.get("selected_skills", {}) and not core_generated_skill(path.name):
            unmanaged.append(path.name)
    if unmanaged:
        raise DomainError("projection_conflict", "Unknown manually added skills at Qorqut-managed names require reconciliation", details={"skills": unmanaged})
    # /role is intentionally short. Never replace or shadow an owner's command,
    # or silently adopt supporting files added to the managed procedure.
    legacy_role = workspace / ".claude/commands/role.md"
    if path_has_symlink(legacy_role) or legacy_role.exists():
        raise DomainError("projection_conflict", "Existing /role command requires reconciliation before installing the role skill")
    for directory in role_workflow.SKILL_DIRECTORIES:
        path = workspace / directory
        if path_has_symlink(path) or (path.exists() and not path.is_dir()):
            raise DomainError("projection_conflict", "The managed role skill must be a local directory")
        for child in path.rglob("*"):
            if child.is_symlink() or child.is_file() and child.relative_to(workspace).as_posix() not in old.get("files", {}):
                raise DomainError("projection_conflict", "Unmanaged role skill files require reconciliation")
    if not registration and not migrate_legacy:
        for path in (workspace / ".claude/settings.json", workspace / ".claude/settings.local.json", workspace / ".mcp.json", workspace / ".codex/config.toml", workspace / ".codex/hooks.json", workspace / "AGENTS.md"):
            if path.exists():
                raise DomainError("projection_conflict", "Existing project configuration needs an explicit migration preview")
    registration_id = registration["id"] if registration else new_id()
    instance_guidance = seed_instance_guidance(resolver, data)
    projection = build_projection(resolver, config, registration_id=registration_id, endpoint=endpoint,
        interpreter=interpreter, capture_transcripts=capture_transcripts,
        checkpoint_protocol=checkpoint_protocols(versions, reports),
        interpreter_validator=interpreter_validator, codex_permission_mode=codex_permission_mode,
        previous_connections=old.get("prepared_connections", old.get("external_connections", [])))
    generated_registration = json.loads(projection.files[".qorqut/registration.json"])
    compatibility["durable_delegation_enabled"] = generated_registration["durable_delegation_enabled"]
    if registration and not migrate_legacy:
        for name in projection.files:
            if file_runtime(name) == "codex" and name not in old.get("files", {}) and (workspace / name).exists():
                raise DomainError("projection_conflict", "Existing Codex files need explicit migration before Qorqut can manage them")
    credential_id = registration["credential_id"] if registration else None
    previous_credential_id = credential_id
    token = None
    if registration and credential_id:
        with resolver.database.read() as uow:
            credential = uow.repository("credentials").get(credential_id)
        if credential and credential["principal_id"] != principal.id:
            raise DomainError("registration_conflict", "Prepared credential belongs to a different principal")
        try:
            token = auth_headers({"workspace": str(workspace), "credential_file": ".qorqut/credential"})["Authorization"][7:]
        except DomainError:
            token = None
        if (not credential or credential["revoked_at"] or credential["expires_at"] and credential["expires_at"] <= utc_now()
            or token is None or hashlib.sha256(token.encode()).hexdigest() != credential["token_hash"]):
            token = None
    if token is None:
        token, credential_id = secrets.token_urlsafe(48), new_id()
        with resolver.database.transaction() as uow:
            Application.ensure_writes_open(uow)
            uow.repository("credentials").insert({"id": credential_id, "principal_id": principal.id,
                "token_hash": hashlib.sha256(token.encode()).hexdigest(), "metadata_json": {"registration_id": registration_id}})
            if previous_credential_id:
                previous = uow.repository("credentials").get(previous_credential_id)
                if previous and not previous["revoked_at"]:
                    uow.repository("credentials").update(previous_credential_id, {"revoked_at": utc_now()}, expected_revision=previous["revision"])
    safe_io.mkdir(workspace)
    for path in projection.metadata.get("business_code_roots", []):
        code_root = Path(path)
        if path_has_symlink(code_root):
            raise DomainError("projection_conflict", "Project code directory cannot follow symlinks")
        safe_io.mkdir(code_root)
    for directory in ("notes", "work", ".qorqut/delivery"):
        safe_io.mkdir(workspace / directory)
    for name in old.get("files", {}):
        if name not in projection.files and (name == ".qorqut/browser" or managed_skill_file(name)
                                             or managed_integration_file(name)):
            target = workspace / relative_path(name)
            if target.is_symlink():
                raise DomainError("projection_conflict", "Managed skill was replaced with a symlink")
            safe_io.unlink(target, missing_ok=True)
    for path in managed_skill_directories(workspace):
        if path.name in old.get("selected_skills", {}) and path.name not in projection.metadata["selected_skills"]:
            safe_io.remove_empty_tree(path)
    for name, body in projection.files.items():
        _write_file(workspace / name, body, private=name.startswith(".qorqut/") or name == ".claude/settings.local.json",
                    executable=name == ".qorqut/agentic")
    _write_file(workspace / ".qorqut/credential", (token + "\n").encode(), private=True)
    ignore = workspace / ".gitignore"
    try:
        previous = safe_io.read_bytes(ignore).decode()
    except FileNotFoundError:
        previous = ""
    entries = [".qorqut/", ".mcp.json", ".claude/settings.local.json", ".claude/skills/", ".codex/", ".agents/skills/", "transcripts/"]
    new = previous.rstrip() + "\n" + "\n".join(entry for entry in entries if entry not in previous.splitlines()) + "\n"
    _write_file(ignore, new.lstrip("\n").encode())
    with resolver.database.transaction() as uow:
        Application.ensure_writes_open(uow)
        resolver.check_agentic_revision(data["agentic_catalog_revision"])
        current, _ = resolver.profile(principal, connection=uow.connection)
        if current.revision != data["profile_revision"]:
            raise DomainError("revision_conflict", "Profile changed during preparation; prepare the current revision")
        values = {"principal_id": principal.id, "workspace": str(workspace), "service_url": service_url(endpoint),
                  "credential_id": credential_id, "profile_revision": current.revision,
                  "settings_digest": projection.metadata["settings_digest"], "skills_digest": projection.metadata["skills_digest"],
                  "projection_json": projection.metadata, "compatibility_json": compatibility}
        repository = uow.repository("machine_registrations")
        if registration:
            repository.update(registration_id, values, expected_revision=registration["revision"])
        else:
            repository.insert({"id": registration_id, **values})
    result = resolver.resolve_agent_config(principal)
    return {"agent_id": principal.id, "profile_revision": data["profile_revision"], "effective_config_id": result.effective_config_id,
            "workspace": str(workspace), "connection_status": projection.metadata["connection_status"],
            "instance_guidance": instance_guidance, **result.to_dict()["preparation"]}


def prepare_claude_agent(resolver, principal, *, claude_version, **kwargs):
    """Compatibility entry point for existing callers and pinned installations."""
    if not compatibility_report(claude_version)["supported"]:
        raise DomainError("unsupported_claude", "Installed Claude Code lacks the supported preparation baseline")
    return prepare_agent(resolver, principal, versions={"claude-code": claude_version}, **kwargs)
