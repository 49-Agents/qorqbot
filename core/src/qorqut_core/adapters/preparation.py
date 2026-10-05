"""Generated projections only. Authored files and native credentials are never read."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import shlex

from .capabilities import require_profile
from .managed_io import ManagedTree
from .types import AdapterError, PreparationInput, PreparationResult, Provider

PREFIX = ".qorqut/adapters"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "Stop", "SessionEnd")


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def instructions(data: PreparationInput) -> bytes:
    return (f"# Generated Qorqut instructions\n\nAgent UUID: {data.run.agent_id}\n"
        f"Conversation UUID: {data.run.conversation_id}\n\n"
        "Read persona.md in this workspace for your authored identity and instructions. "
        "Read memory.md and maintain current decisions, findings and next actions there or in notes/. "
        "These files belong to the same agent across native CLI changes.\n\n"
        "Use qorqut-core MCP for agent coordination, messages, status and history retrieval. "
        "Captured history is available through history_search, history_get and history_raw_get "
        "when those operations are advertised. Visible conversation text is a projection of history.\n\n"
        "Use the separate qorqut-browser MCP for browser actions. Call browser_request_input "
        "when the user must sign in, enter data or take another action in your browser. "
        "Provide the action prompt, wait for the saved request's completion and control readiness "
        "before resuming managed browser actions. Completion does not prove authentication succeeded.\n\n"
        "Call files_reference on qorqut-core with a path when the user should see, review or be "
        "notified about a file. The result records a saved reference; it does not prove that "
        "the user opened the file.\n\n"
        "Native authentication and workspace/hook trust are required setup states. "
        "Generated configuration does not establish a login or verify access. "
        "Native tools remain available.\n").encode()


def _hooks(data: PreparationInput, provider: Provider, binding_path: Path):
    if not data.hook_sink_argv:
        return {}
    # The sink receives an immutable per-run binding file through its environment.
    # It must validate native process ownership and persist before forwarding.
    command = shlex.join(("env", "QORQUT_ADAPTER_BINDING_FILE=" + str(binding_path), *data.hook_sink_argv))
    events = (*EVENTS, "PostToolUseFailure") if provider == "claude" else (*EVENTS, "Interrupt")
    return {event: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
        for event in events}


def _codex_config(data: PreparationInput) -> bytes:
    # This deliberately small TOML projection has only strings, booleans and tables.
    # JSON string quoting is compatible with TOML basic strings for these inputs.
    lines = ['approval_policy = "never"', 'sandbox_mode = "danger-full-access"', ""]
    for name, endpoint in (("qorqut-core", data.core), ("qorqut-browser", data.browser)):
        table = "mcp_servers." + json.dumps(name)
        lines.extend((f"[{table}]", "url = " + json.dumps(endpoint.url, ensure_ascii=False),
            # Browser unavailability must not make core's registration disappear.
            "required = false", 'default_tools_approval_mode = "approve"',
            f"[{table}.http_headers]"))
        lines.extend(json.dumps(key, ensure_ascii=False) + " = " + json.dumps(value, ensure_ascii=False)
            for key, value in endpoint.headers)
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def prepare(provider: Provider, version: str, data: PreparationInput) -> PreparationResult:
    require_profile(provider, version)
    directory = f"{PREFIX}/{provider}"
    binding_relative = f"{directory}/runs/{data.run.run_id}.json"
    binding_path = data.roots.workspace / binding_relative
    binding = {"provider": provider, "cli_version": version,
        "run": data.run.model_dump(mode="json"), "roots": data.roots.model_dump(mode="json")}
    hooks = _hooks(data, provider, binding_path)
    files: dict[tuple[str, str], bytes] = {
        ("workspace", "CLAUDE.md" if provider == "claude" else "AGENTS.md"): instructions(data),
        ("workspace", binding_relative): json_bytes(binding),
    }
    if provider == "claude":
        servers = {name: {"type": "http", "url": endpoint.url, "headers": dict(endpoint.headers)}
            for name, endpoint in (("qorqut-core", data.core), ("qorqut-browser", data.browser))}
        files["workspace", f"{directory}/mcp.json"] = json_bytes({"mcpServers": servers})
        files["workspace", f"{directory}/settings.json"] = json_bytes({
            "permissions": {"defaultMode": "bypassPermissions"}, "hooks": hooks})
    else:
        files["home", ".codex/config.toml"] = _codex_config(data)
        files["home", ".codex/hooks.json"] = json_bytes({"hooks": hooks})
    identity = {"agent_id": str(data.run.agent_id), "conversation_id": str(data.run.conversation_id),
        "roots": data.roots.model_dump(mode="json")}
    marker = f"{PREFIX}/identity.json"
    manifest_name = f"{directory}/preparation.json"
    with ExitStack() as stack:
        trees = {name: stack.enter_context(ManagedTree(getattr(data.roots, name)))
            for name in ("home", "workspace")}
        # Path order also avoids deadlock for accidentally exchanged root inputs.
        for name in sorted(trees, key=lambda name: str(trees[name].root)):
            stack.enter_context(trees[name].lock(f"{PREFIX}/prepare.lock"))
        for tree in trees.values():
            prior = tree.read(marker)
            if prior is not None and prior != json_bytes(identity):
                raise AdapterError("Managed root belongs to a different identity or root binding")
        prior_manifest = trees["workspace"].read(manifest_name)
        manifest = json.loads(prior_manifest) if prior_manifest else {}
        if manifest and (manifest.get("identity") != identity or manifest.get("provider") != provider):
            raise AdapterError("Preparation manifest binding mismatch")
        old_hashes = manifest.get("files", {})
        # Validate every destination before touching any generated contents.
        for (root, path), body in files.items():
            prior = trees[root].read(path)
            if prior is None or prior == body:
                continue
            if path == binding_relative:
                raise AdapterError("A run binding is immutable")
            if digest(prior) != old_hashes.get(root + ":" + path):
                raise AdapterError("Refusing to replace authored or changed generated file: " + path)
        for tree in trees.values():
            if tree.read(marker) is None:
                tree.atomic_write(marker, json_bytes(identity))
        for (root, path), body in files.items():
            if trees[root].read(path) != body:
                trees[root].atomic_write(path, body)
        # Install metadata last. A crash leaves complete old/new files; identical retry recovers.
        hashes = {**old_hashes, **{root + ":" + path: digest(body) for (root, path), body in files.items()}}
        trees["workspace"].atomic_write(manifest_name, json_bytes({"schema_version": 1,
            "provider": provider, "cli_version": version, "identity": identity, "files": hashes,
            "native_verified": False}))
    gaps = ("native_mcp_discovery_unverified", "authentication_requires_setup", "native_hooks_unverified")
    if not data.hook_sink_argv:
        gaps += ("durable_hook_sink_not_configured",)
    return PreparationResult(provider=provider, run=data.run, roots=data.roots,
        generated_paths=(*tuple(getattr(data.roots, root) / path for root, path in files),
            data.roots.home / marker, data.roots.workspace / marker,
            data.roots.workspace / manifest_name),
        binding_path=binding_path, gaps=gaps)
