"""Real native projection for one owner-selected agent; no WhatsApp/network use."""
import hashlib
import json
from pathlib import Path
import tomllib
from uuid import uuid4

import pytest

from bazarlyk.bootstrap import whatsapp
from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.bootstrap.seed import apply_initial_plan
from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal
from host_foundation_support import build_instance, create_worker, register_sources

VERSIONS = {"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.156.1"}


@pytest.fixture
def prepared_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("QORQUT_INSTALLATION_CONFIG", raising=False)
    # Native interpreter health is an independent installed-runtime contract.
    # Exercise real registration/projection writes with a synthetic healthy helper.
    from bazarlyk.bootstrap import diagnostics
    monkeypatch.setattr(diagnostics, "prepared_helpers", lambda *_: {"ready": True})
    instance, _ = build_instance(tmp_path)
    register_sources(instance, instance.business, "first")
    ctx = InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES,
                            allowed_business_ids=frozenset({instance.business}), source="dashboard")
    with instance.database.read() as uow:
        plan = instance.runtime.publisher.prepare(ctx, {"source_id": "ordubashy"},
            InvocationOptions(idempotency_key="seed-ordubashy"), uow, "role")
    pin = apply_initial_plan(instance.database, ctx, plan, instance.runtime.publisher,
                            instance.runtime.publisher.resolve_file_target, instance.runtime.locks)["artifact"]
    result = instance.invoke(ctx, "agents_create", {"role": pin, "plugins": [], "name": "reader",
        "display_name": "Reader", "assignment_brief": "Synthetic personal reader",
        "scope": {"kind": "ecosystem", "allowed_business_ids": [instance.business], "global_capabilities": ["read_global_context"]}})
    assert result.status == "ok", result.error
    agent = result.data["agent_id"]
    root = instance.paths.instance_root
    binary = root / "runtime/connectors/whatsapp-personal" / ("a" * 40) / "whatsapp-personal"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 91\n")
    binary.chmod(0o755)
    binding = root / "config/whatsapp-personal.json"
    binding.parent.mkdir(exist_ok=True)
    binding.write_text(json.dumps({"version": 1, "agent_id": agent, "binary": str(binary),
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "socket": str(whatsapp.socket_path(root))}))
    binding.chmod(0o600)
    return instance, agent, binary, binding


def prepare(instance, agent):
    return prepare_agent(instance.runtime.resolver, Principal(agent, "agent"),
                         endpoint="http://127.0.0.1:9988", versions=VERSIONS)


def configs(workspace):
    workspace = Path(workspace)
    return (json.loads((workspace / ".mcp.json").read_text())["mcpServers"],
            tomllib.loads((workspace / ".codex/config.toml").read_text())["mcp_servers"],
            json.loads((workspace / ".claude/settings.local.json").read_text())["permissions"])


def test_exact_read_connection_and_unrelated_servers_survive_refresh(prepared_instance):
    instance, agent, binary, binding = prepared_instance
    result = prepare(instance, agent)
    assert result["ready"] and result["connection_status"][whatsapp.NAME] == {"prepared": True}
    workspace = Path(result["workspace"])
    claude, codex, permissions = configs(workspace)
    args = ["mcp", "--socket", str(whatsapp.socket_path(instance.paths.instance_root))]
    assert claude[whatsapp.NAME] == {"type": "stdio", "command": str(binary), "args": args}
    assert codex[whatsapp.NAME]["enabled_tools"] == ["whatsapp_read"]
    assert codex[whatsapp.NAME]["args"] == args and codex[whatsapp.NAME]["required"] is False
    assert [r for r in permissions["allow"] if "whatsapp" in r] == ["mcp__whatsapp-personal__whatsapp_read"]
    for file in ("AGENTS.md", "CLAUDE.md"):
        assert whatsapp.GUIDANCE in (workspace / file).read_text()
    document = json.loads((workspace / ".mcp.json").read_text())
    document["mcpServers"]["unrelated"] = {"command": "/bin/false", "args": []}
    (workspace / ".mcp.json").write_text(json.dumps(document))
    with (workspace / ".codex/config.toml").open("a") as out:
        out.write('\n[mcp_servers.unrelated]\ncommand = "/bin/false"\n')
    assert prepare(instance, agent)["ready"]
    assert "unrelated" in configs(workspace)[0] and "unrelated" in configs(workspace)[1]
    # Removing the owner binding cleans up only this managed server.
    binding.unlink()
    assert prepare(instance, agent)["ready"]
    for servers in configs(workspace)[:2]:
        assert whatsapp.NAME not in servers and "unrelated" in servers


def test_other_global_agent_and_different_uuid_get_no_reader(prepared_instance):
    instance, agent, binary, binding = prepared_instance
    other = create_worker(instance, "colleague")          # an independent peer, not the selected reader
    assert whatsapp.NAME not in configs(prepare(instance, other)["workspace"])[0]
    value = json.loads(binding.read_text()); value["agent_id"] = str(uuid4())
    binding.write_text(json.dumps(value))
    assert whatsapp.NAME not in configs(prepare(instance, agent)["workspace"])[0]


def test_missing_binary_removes_managed_reader_and_tampering_is_refused(prepared_instance):
    instance, agent, binary, binding = prepared_instance
    workspace = Path(prepare(instance, agent)["workspace"])
    binary.write_text("changed")
    with pytest.raises(DomainError, match="integrity"):
        prepare(instance, agent)
    binary.unlink()
    result = prepare(instance, agent)
    assert result["connection_status"][whatsapp.NAME]["prepared"] is False
    assert whatsapp.NAME not in configs(workspace)[0]


def test_unmanaged_server_collision_is_refused_before_writes(prepared_instance):
    instance, agent, binary, binding = prepared_instance
    original = binding.read_bytes(); binding.unlink()
    workspace = Path(prepare(instance, agent)["workspace"])
    document = json.loads((workspace / ".mcp.json").read_text())
    document["mcpServers"][whatsapp.NAME] = {"command": "/bin/false"}
    (workspace / ".mcp.json").write_text(json.dumps(document))
    before = (workspace / ".mcp.json").read_bytes()
    binding.write_bytes(original); binding.chmod(0o600)
    with pytest.raises(DomainError, match="unmanaged"):
        prepare(instance, agent)
    assert (workspace / ".mcp.json").read_bytes() == before
