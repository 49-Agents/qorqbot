"""Managed projections, preserving native additions only in claimed Codex config."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import shlex
import tomllib

from .capabilities import require_profile
from .managed_io import ManagedTree, validate_generated_size
from .types import AdapterError, PreparationInput, PreparationResult, Provider

PREFIX = ".qorqut/adapters"
CODEX_CONFIG = ".codex/config.toml"
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


def _typed(value):
    if isinstance(value, dict):
        return (dict, tuple(sorted((key, _typed(item)) for key, item in value.items())))
    if isinstance(value, list):
        return (list, tuple(_typed(item) for item in value))
    return (type(value), value)  # In particular, False is not the integer 0.


def _codex_owned(content: bytes):
    try:
        config = tomllib.loads(content.decode("utf-8"))
        return _typed({"approval_policy": config["approval_policy"],
            "sandbox_mode": config["sandbox_mode"],
            "mcp_servers": {name: config["mcp_servers"][name]
                for name in ("qorqut-core", "qorqut-browser")}})
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError):
        raise AdapterError("Invalid or missing owned Codex configuration") from None


def _check_codex_config(tree: ManagedTree, expected: bytes | None):
    if tree.read(CODEX_CONFIG) != expected:
        raise AdapterError("Codex configuration changed during preparation")


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
        files["home", CODEX_CONFIG] = _codex_config(data)
        files["home", ".codex/hooks.json"] = json_bytes({"hooks": hooks})
    identity = {"agent_id": str(data.run.agent_id), "conversation_id": str(data.run.conversation_id),
        "roots": data.roots.model_dump(mode="json")}
    marker = f"{PREFIX}/identity.json"
    manifest_name = f"{directory}/preparation.json"
    identity_bytes = json_bytes(identity)
    # This is the current projection, not an index of historical run bindings.
    hashes = {root + ":" + path: digest(body) for (root, path), body in files.items()}
    metadata = {"schema_version": 1,
        "provider": provider, "cli_version": version, "identity": identity, "files": hashes,
        "native_verified": False}
    if provider == "codex":
        metadata["codex_config_preserved"] = False
    manifest_bytes = json_bytes(metadata)
    # Reject oversized outputs before installing any part of the projection.
    for body in (*files.values(), identity_bytes, manifest_bytes):
        validate_generated_size(body)
    with ExitStack() as stack:
        trees = {name: stack.enter_context(ManagedTree(getattr(data.roots, name)))
            for name in ("home", "workspace")}
        identities = {name: tree.identity for name, tree in trees.items()}
        if identities["home"] == identities["workspace"]:
            raise AdapterError("HOME and workspace refer to the same filesystem directory")
        # Order by opened filesystem identity, independent of path aliases/roles.
        for name in sorted(trees, key=identities.__getitem__):
            stack.enter_context(trees[name].lock(f"{PREFIX}/prepare.lock"))
        existing_identity = True
        for tree in trees.values():
            prior = tree.read(marker)
            if prior is not None and prior != identity_bytes:
                raise AdapterError("Managed root belongs to a different identity or root binding")
            existing_identity = existing_identity and prior is not None
        prior_manifest = trees["workspace"].read(manifest_name)
        try:
            manifest = json.loads(prior_manifest) if prior_manifest is not None else {}
        except ValueError:
            raise AdapterError("Invalid preparation manifest") from None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files", {}), dict):
            raise AdapterError("Invalid preparation manifest")
        if manifest and (manifest.get("identity") != identity or manifest.get("provider") != provider):
            raise AdapterError("Preparation manifest binding mismatch")
        old_hashes = manifest.get("files", {})
        preserved = manifest.get("codex_config_preserved", False) if provider == "codex" else False
        if type(preserved) is not bool:
            raise AdapterError("Invalid codex_config_preserved in preparation manifest")
        config_before = None
        # Validate every destination before touching any generated contents.
        for (root, path), body in files.items():
            prior = trees[root].read(path)
            old_hash = old_hashes.get(root + ":" + path)
            if provider == "codex" and (root, path) == ("home", CODEX_CONFIG):
                config_before = prior
                if prior is None:
                    preserved = False
                elif preserved or (digest(prior) != old_hash and (prior != body or old_hash is not None)):
                    claimed = (existing_identity and type(manifest.get("schema_version")) is int
                        and manifest["schema_version"] == 1 and manifest.get("cli_version") == version
                        and manifest.get("native_verified") is False
                        and isinstance(old_hash, str) and re.fullmatch(r"[0-9a-f]{64}", old_hash))
                    if not claimed:
                        raise AdapterError("Refusing to preserve unclaimed Codex configuration")
                    if _codex_owned(prior) != _codex_owned(body):
                        raise AdapterError("Refusing to replace changed owned Codex configuration")
                    # Retain all native/user bytes, including on later retries
                    # whose manifest now carries the preserved file's hash.
                    files[root, path] = prior
                    hashes[root + ":" + path] = digest(prior)
                    preserved = True
                    continue
            if prior is None or prior == body:
                continue
            if path == binding_relative:
                # Always compare the selected binding's actual bytes, even when
                # its run is no longer listed in the current manifest.
                raise AdapterError("A run binding is immutable")
            if digest(prior) != old_hash:
                raise AdapterError("Refusing to replace authored or changed generated file: " + path)
        if provider == "codex":
            metadata["codex_config_preserved"] = preserved
            _check_codex_config(trees["home"], config_before)
        manifest_bytes = json_bytes(metadata)
        validate_generated_size(manifest_bytes)
        for tree in trees.values():
            if tree.read(marker) is None:
                tree.atomic_write(marker, identity_bytes)
        for (root, path), body in files.items():
            if provider == "codex" and (root, path) == ("home", CODEX_CONFIG):
                _check_codex_config(trees[root], config_before)
                if not preserved and config_before != body:
                    trees[root].atomic_write(path, body)
                continue  # Never overwrite a native file to undo a racing edit.
            if trees[root].read(path) != body:
                trees[root].atomic_write(path, body)
        # Install metadata last. A crash leaves complete old/new files; identical retry recovers.
        if provider == "codex":
            _check_codex_config(trees["home"], files["home", CODEX_CONFIG])
        trees["workspace"].atomic_write(manifest_name, manifest_bytes)
    gaps = ("native_mcp_discovery_unverified", "authentication_requires_setup", "native_hooks_unverified")
    if not data.hook_sink_argv:
        gaps += ("durable_hook_sink_not_configured",)
    return PreparationResult(provider=provider, run=data.run, roots=data.roots,
        generated_paths=(*tuple(getattr(data.roots, root) / path for root, path in files),
            data.roots.home / marker, data.roots.workspace / marker,
            data.roots.workspace / manifest_name),
        binding_path=binding_path, gaps=gaps)
