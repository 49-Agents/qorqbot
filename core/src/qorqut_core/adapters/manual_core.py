"""Agent-local core MCP and instruction entries; no launch or native preferences."""

from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import tomllib

from .capabilities import require_profile
from .managed_io import ManagedTree, validate_generated_size
from .manual_hooks import ManualBinding, NativeProgram
from .preparation import CODEX_CONFIG, PREFIX, _typed, json_bytes
from .types import AdapterError, ManagedRoots, MCPBinding

MANIFEST = ".qorqut/manual-core/preparation.json"
BEGIN = "<!-- QORQUT CORE INSTRUCTIONS BEGIN -->"
END = "<!-- QORQUT CORE INSTRUCTIONS END -->"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError


def _object(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant) if raw is not None else {}
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise AdapterError("Invalid core configuration or ownership record") from None


def _instructions(binding):
    return (f"{BEGIN}\n"
        f"Agent: {binding.agent_id}. Conversation: {binding.conversation_id}.\n"
        "Read persona.md for your identity and instructions. Read memory.md and relevant files in "
        "notes/, research/ and data/; maintain useful findings there across CLI sessions.\n"
        "Use the advertised qorqut-core MCP tools for agent coordination, messages, status and "
        "conversation retrieval. Native tools remain available.\n"
        "If a hook explicitly reports that another session is connected to this agent, tell the "
        "user and wait until that session is closed. A generic connection failure is not a session conflict.\n"
        "Installed configuration does not establish native login, trust, MCP connection or input readiness.\n"
        f"{END}\n")


def _instruction_file(raw, previous, desired):
    content = raw or b""
    block = desired.encode("utf-8")
    old = previous.encode("utf-8") if previous is not None else block
    if previous is not None or BEGIN.encode() in content or END.encode() in content:
        if (content.count(old) != 1 or content.count(BEGIN.encode()) != 1
                or content.count(END.encode()) != 1):
            raise AdapterError("Owned core instruction block was changed")
        return content.replace(old, block, 1)
    return content + (b"\n\n" if content else b"") + block


def _claude(raw, previous, desired):
    config = _object(raw)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise AdapterError("Invalid Claude MCP server configuration")
    present = servers.get("qorqut-core")
    if previous is not None:
        if _typed(present) != _typed(previous):
            raise AdapterError("Owned Claude core entry was changed")
    elif "qorqut-core" in servers and _typed(present) != _typed(desired):
        raise AdapterError("Unowned Claude core entry conflicts")
    if _typed(present) == _typed(desired):
        return raw
    servers["qorqut-core"] = desired
    return json_bytes(config)


def _toml(raw):
    try:
        config = tomllib.loads((raw or b"").decode("utf-8"))
        if not isinstance(config.get("mcp_servers", {}), dict):
            raise ValueError
        return config
    except (ValueError, UnicodeError, RecursionError):
        raise AdapterError("Invalid Codex core configuration") from None


def _codex(raw, previous, desired):
    config = _toml(raw)
    servers = config.get("mcp_servers", {})
    present = servers.get("qorqut-core")
    if previous is not None:
        if _typed(present) != _typed(previous):
            raise AdapterError("Owned Codex core table was changed")
        if _typed(previous) != _typed(desired):
            raise AdapterError("Replacing an owned Codex core table is unsupported")
    elif "qorqut-core" in servers and _typed(present) != _typed(desired):
        raise AdapterError("Unowned Codex core table conflicts")
    if _typed(present) == _typed(desired):
        return raw  # Preserve every byte, including native-added trust tables.
    table = 'mcp_servers."qorqut-core"'
    lines = [f"[{table}]", "url = " + json.dumps(desired["url"], ensure_ascii=False),
             "required = false", f"[{table}.http_headers]"]
    lines.extend(json.dumps(key, ensure_ascii=False) + " = " + json.dumps(value, ensure_ascii=False)
                 for key, value in desired["http_headers"].items())
    body = (raw or b"") + b"\n" + ("\n".join(lines) + "\n").encode("utf-8")
    # Inline/otherwise closed native table shapes may not accept an append.
    # Refuse those shapes without attempting a whole-document rewrite.
    expected = {**config, "mcp_servers": {**servers, "qorqut-core": desired}}
    if _typed(_toml(body)) != _typed(expected):
        raise AdapterError("Appending the Codex core table is unsupported")
    return body


def prepare_manual_core(roots: ManagedRoots, binding: ManualBinding, core: MCPBinding,
                        programs: tuple[NativeProgram, ...]) -> tuple[Path, ...]:
    roots, binding, core = (ManagedRoots.model_validate(roots), ManualBinding.model_validate(binding),
                            MCPBinding.model_validate(core))
    programs = tuple(NativeProgram.model_validate(program) for program in programs)
    if not programs or len(programs) > 2 or len({p.provider for p in programs}) != len(programs):
        raise AdapterError("Supply distinct explicit native programs")
    for program in programs:
        require_profile(program.provider, program.cli_version)
    identity = {"agent_id": str(binding.agent_id), "conversation_id": str(binding.conversation_id),
                "roots": roots.model_dump(mode="json")}
    identity_bytes = json_bytes(identity)
    marker = f"{PREFIX}/identity.json"
    with ExitStack() as stack:
        trees = {name: stack.enter_context(ManagedTree(getattr(roots, name))) for name in ("home", "workspace")}
        identities = {name: tree.identity for name, tree in trees.items()}
        if identities["home"] == identities["workspace"]:
            raise AdapterError("HOME and workspace must be distinct directories")
        for name in sorted(trees, key=identities.__getitem__):
            stack.enter_context(trees[name].lock(f"{PREFIX}/prepare.lock"))
        for tree in trees.values():
            prior = tree.read(marker)
            if prior is not None and prior != identity_bytes:
                raise AdapterError("Managed roots belong to another binding")
        manifest_before = trees["workspace"].read(MANIFEST)
        manifest = _object(manifest_before)
        if manifest and (manifest.get("identity") != identity or type(manifest.get("schema_version")) is not int
                         or manifest["schema_version"] != 1):
            raise AdapterError("Manual core ownership binding changed")
        owned = manifest.get("providers", {})
        if not isinstance(owned, dict):
            raise AdapterError("Invalid manual core ownership record")
        files = {}
        for program in programs:
            provider = program.provider
            previous = owned.get(provider)
            if provider in owned and (not isinstance(previous, dict) or set(previous) != {"core", "instructions"}
                    or not isinstance(previous["core"], dict) or not isinstance(previous["instructions"], str)):
                raise AdapterError("Invalid manual core ownership record")
            if provider == "claude":
                root, path, merge = "workspace", ".mcp.json", _claude
                desired = {"type": "http", "url": core.url, "headers": dict(core.headers)}
            else:
                root, path, merge = "home", CODEX_CONFIG, _codex
                desired = {"url": core.url, "required": False, "http_headers": dict(core.headers)}
            before = trees[root].read(path)
            files[root, path] = (before, merge(before, previous["core"] if previous is not None else None, desired))
            instruction_path = "CLAUDE.md" if provider == "claude" else "AGENTS.md"
            before = trees["workspace"].read(instruction_path)
            block = _instructions(binding)
            files["workspace", instruction_path] = (before, _instruction_file(
                before, previous["instructions"] if previous is not None else None, block))
            owned[provider] = {"core": desired, "instructions": block}
        manifest_body = json_bytes({"schema_version": 1, "identity": identity, "providers": owned})
        files["workspace", MANIFEST] = (manifest_before, manifest_body)
        for body in (identity_bytes, *(body for _, body in files.values())):
            validate_generated_size(body)
        for (root, path), (before, _) in files.items():
            if trees[root].read(path) != before:
                raise AdapterError("Core configuration changed during preparation")
        for tree in trees.values():
            if tree.read(marker) is None:
                tree.atomic_write(marker, identity_bytes)
        for (root, path), (before, body) in files.items():
            if trees[root].read(path) != before:
                raise AdapterError("Core configuration changed during preparation")
            if before != body:
                trees[root].atomic_write(path, body)
        for (root, path), (_, body) in files.items():
            if trees[root].read(path) != body:
                raise AdapterError("Core configuration changed during preparation")
    return tuple(getattr(roots, root) / path for root, path in files if path != MANIFEST)
