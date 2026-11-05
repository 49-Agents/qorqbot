"""Synthetic Email Management v3 package and roles for preparation checks.

Everything is generated inside a pytest ``tmp_path``. The fixture mirrors the v3 contract of the instruction package (version
3.0.0, no tools, ``owner_input`` approval, a native skill with the ``{{EMAIL_MANAGEMENT_HOME}}`` placeholder and an
``instance-template/``) with invented accounts; it is never the real package, and no real role or package source is edited."""

from __future__ import annotations

import json
from pathlib import Path

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.bootstrap.seed import apply_initial_plan
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal
from host_foundation_support import WORKER_ROLE, publish_role, synthetic_ordubashy

PLACEHOLDER = "{{EMAIL_MANAGEMENT_HOME}}"
PLUGIN_FILES = {
    "plugin.yaml": """schema_version: 1
id: email-management
version: 3.0.0
scope_kinds: [ecosystem]
description: Synthetic Email Management guidance that carries no tools.
config_schema: config.schema.yaml
tools: tools.yaml
rules: rules.yaml
skills:
  - id: email-management
    path: skills/email-management/SKILL.md
""",
    "config.schema.yaml": "type: object\nadditionalProperties: false\nproperties: {}\n",
    "tools.yaml": "tools: {}\n",
    "rules.yaml": "channel: email\napproval: owner_input\n",
    "skills/email-management/SKILL.md": f"""---
name: bazarlyk-plugin-email-management
description: Synthetic entry; read the instance Email Management guidance before any mailbox work.
---
# Email Management

Read `{PLACEHOLDER}/SKILL.md` first, then the inbox and provider notes it references under `{PLACEHOLDER}`.
""",
    "skills/email-management/instance-template/SKILL.md": "# Email guidance\n\nExample account: owner@example.test (synthetic).\nSee instructions.md.\n",
    "skills/email-management/instance-template/instructions.md": "# Instructions\n\nSynthetic example preference: ask the owner before sending.\n",
    "skills/email-management/instance-template/inboxes/README.md": "# Inboxes\n\nList each inbox the owner wants handled.\n",
    "skills/email-management/instance-template/providers/gmail.md": "# Gmail\n\nUse the native Gmail connector only when it is exposed to this session.\n",
    "skills/email-management/instance-template/providers/fastmail.md": "# Fastmail\n\nUse fastmail_* operations only when the optional provider is installed.\n",
}


def write_package(root: Path, overrides: dict | None = None) -> Path:
    """The synthetic v3 package source tree; ``overrides`` replaces (or, with None, removes) individual files."""
    files = {**PLUGIN_FILES, **(overrides or {})}
    for name, body in files.items():
        if body is None:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def configure_sources(monkeypatch, tmp_path: Path, package_root: Path) -> None:
    """Select a source map holding the three roles and the synthetic package, before the runtime is built."""
    sources = {role: {"kind": "role", "path": "qorqut-agentic/src/qorqut_agentic/resources/defaults", "manifest": f"roles/imported-roles/native/roles/{role}/role.yaml"}
               for role in ("ordubashy", "global-worker", "learning-applier")}
    sources["email-management"] = {"kind": "plugin", "path": str(package_root), "manifest": "plugin.yaml"}
    target = tmp_path / "package-sources.json"
    target.write_text(json.dumps(sources))
    monkeypatch.setenv("QORQUT_PACKAGE_SOURCES", str(target))


def owner(instance) -> InvocationContext:
    return InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES,
                             allowed_business_ids=frozenset({instance.business}), source="dashboard")


def publish_plugin(instance, source_id="email-management") -> dict:
    ctx = owner(instance)
    publisher = instance.runtime.publisher
    with instance.database.read() as uow:
        plan = publisher.prepare(ctx, {"source_id": source_id}, InvocationOptions(idempotency_key=f"seed-{source_id}"), uow, "plugin")
    return apply_initial_plan(instance.database, ctx, plan, publisher, publisher.resolve_file_target, instance.runtime.locks)["artifact"]


def create_agent(instance, role: str, name: str, *, plugins=None) -> str:
    """Create a global agent through ``agents_create``; omitting ``plugins`` leaves the host defaults in force."""
    arguments = {"role": publish_role(instance, role), "name": name, "display_name": name.title(),
                 "assignment_brief": "Synthetic fixture agent.",
                 "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}}
    if plugins is not None:
        arguments["plugins"] = plugins
    created = instance.invoke(owner(instance), "agents_create", arguments)
    assert created.status == "ok", created.error
    return created.data["agent_id"]


def grants(instance, agent_id: str) -> list[dict]:
    with instance.database.read() as uow:
        row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
    value = row[0]
    return (json.loads(value) if isinstance(value, (str, bytes)) else value)["plugins"]


__all__ = ["PLACEHOLDER", "WORKER_ROLE", "write_package", "configure_sources", "synthetic_ordubashy", "owner", "publish_plugin",
           "create_agent", "grants"]
