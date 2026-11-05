"""Focused gates for durable delegation and the delegating parent's Codex defaults."""

import tomllib

from bazarlyk.bootstrap.codex_config import configuration, role_model_default
from bazarlyk.bootstrap.preparation import durable_delegation_enabled


def profile(*, version="1.34.0", role="ordubashy", scope_kind="ecosystem",
            capabilities=("manage_delegated_assignments",)):
    return {"role": {"id": role, "version": version},
            "scope": {"kind": scope_kind, "global_capabilities": list(capabilities)}}


def test_durable_delegation_requires_role_skill_version_and_explicit_capability():
    assert durable_delegation_enabled(profile(), delegate_operations=True)
    assert not durable_delegation_enabled(profile(version="1.33.0"), delegate_operations=True)
    assert not durable_delegation_enabled(profile(capabilities=()), delegate_operations=True)
    assert not durable_delegation_enabled(profile(scope_kind="business"), delegate_operations=True)
    assert not durable_delegation_enabled(profile(role="onbashy"), delegate_operations=True)
    # The retired Global Marketing role has no durable delegation of its own, whatever version or grants it stored.
    assert not durable_delegation_enabled(profile(role="global-marketing", version="1.11.0",
                                                  capabilities=("manage_delegated_assignments", "coordinate_businesses")),
                                          delegate_operations=True)
    assert not durable_delegation_enabled(profile(), delegate_operations=False)


def test_delegating_parent_keeps_role_default_and_child_luna_default():
    common = {"workspace": "/tmp/worker", "endpoint": "http://127.0.0.1:9000",
        "header_command": "headers", "command": "bootstrap", "snapshot_command": "snapshot",
        "protected": (), "read_denied": (), "delegate_operations": True}
    parent_config, _ = configuration(**common, model_default=role_model_default("ecosystem", "ordubashy"))
    parsed = tomllib.loads(parent_config.decode())
    assert (parsed["model"], parsed["model_reasoning_effort"]) == ("gpt-6-luna", "high")
    assert parsed["agents"]["default_subagent_model"] == "gpt-6-luna"
    assert parsed["agents"]["default_subagent_reasoning_effort"] == "low"

    legacy_config, _ = configuration(**common)
    assert "model" not in tomllib.loads(legacy_config.decode())
