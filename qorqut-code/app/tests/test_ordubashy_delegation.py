"""Role-selected native delegation survives preparation without widening grants."""

import json
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace

from bazarlyk.bootstrap.preparation import build_projection
from test_agent_configuration import Fixture


def projection(fixture, data):
    return build_projection(fixture.resolver, SimpleNamespace(to_dict=lambda: data),
        registration_id="synthetic-preview", endpoint="http://127.0.0.1:9988",
        interpreter=sys.executable)


def test_adopted_ordubashy_prepares_luna_defaults_and_skill(tmp_path):
    fixture = Fixture(tmp_path)
    principal = fixture.create("coordinator", "ordubashy")
    data = fixture.resolver.resolve_agent_config(principal).to_dict()
    selected = projection(fixture, data)
    # An older pin without the selected skill must keep its previous behavior.
    old = projection(fixture, {**data, "skills": [skill for skill in data["skills"]
        if skill["name"] != "bazarlyk-role-operations-delegation"]})
    configured = tomllib.loads(selected.files[".codex/config.toml"].decode())
    previous = tomllib.loads(old.files[".codex/config.toml"].decode())
    assert configured.pop("agents") == {
        "enabled": True, "default_subagent_model": "gpt-6-luna",
        "default_subagent_reasoning_effort": "low",
        "max_concurrent_threads_per_session": 3,
    }
    # The role's model default, permissions, MCP identity and reviewed hook definitions stay intact.
    assert configured == previous
    assert (configured["model"], configured["model_reasoning_effort"]) == ("gpt-6-luna", "high")
    assert selected.files[".codex/hooks.json"] == old.files[".codex/hooks.json"]
    assert selected.files["CLAUDE.md"] == old.files["CLAUDE.md"]

    ready = fixture.prepare(principal)
    workspace = Path(ready["workspace"])
    skill = workspace / ".agents/skills/bazarlyk-role-operations-delegation/SKILL.md"
    assert ready["ready"] and skill.is_file()
    role_source = fixture.sources["ordubashy"][1]
    authored_skill = role_source / "skills/operations-delegation/SKILL.md"
    assert skill.read_text() == authored_skill.read_text()
    assert "native tools exposed in the current Codex turn" in " ".join(skill.read_text().split())
    before = (workspace / ".codex/config.toml").read_bytes()
    assert fixture.prepare(principal)["ready"]
    assert (workspace / ".codex/config.toml").read_bytes() == before
    assert tomllib.loads(before.decode())["agents"]["default_subagent_model"] == "gpt-6-luna"
    # The registration remains one ordinary Ordubashy, not a second worker identity.
    registration = json.loads((workspace / ".bazarlyk/registration.json").read_text())
    assert registration["agent_id"] == principal.id


def test_other_roles_and_plugin_skills_do_not_enable_ordubashy_delegation(tmp_path):
    fixture = Fixture(tmp_path)
    for principal in (fixture.create(), fixture.create("founder", "founder"), fixture.create("marketing", "global-marketing")):
        data = fixture.resolver.resolve_agent_config(principal).to_dict()
        data["skills"].append({"name": "bazarlyk-role-operations-delegation",
                               "owner": {"kind": "role", "id": "ordubashy"}})
        assert "agents" not in tomllib.loads(projection(fixture, data).files[".codex/config.toml"].decode())
    data = fixture.resolver.resolve_agent_config(fixture.create("coordinator", "ordubashy")).to_dict()
    for skill in data["skills"]:
        if skill["name"] == "bazarlyk-role-operations-delegation":
            skill["owner"] = {"kind": "plugin", "id": "other"}
    assert "agents" not in tomllib.loads(projection(fixture, data).files[".codex/config.toml"].decode())


def test_prepared_delegation_routes_short_work_to_durable_even_without_dispatch(tmp_path):
    """Startup and selected role agree; an unavailable gate never selects native work."""
    fixture = Fixture(tmp_path)
    principal = fixture.create("durable-default-coordinator", "ordubashy")
    data = fixture.resolver.resolve_agent_config(principal).to_dict()
    # Exercise both generated startup branches against the paired new role artifact.
    for enabled in (False, True):
        scoped = json.loads(json.dumps(data))
        capabilities = scoped["scope"]["global_capabilities"]
        capabilities[:] = [v for v in capabilities if v != "manage_delegated_assignments"]
        if enabled:
            capabilities.append("manage_delegated_assignments")
        selected = projection(fixture, scoped)
        startup = selected.files["AGENTS.md"].decode()
        configuration = tomllib.loads(selected.files[".codex/config.toml"].decode())
        assert "including short isolated chores" in startup
        assert "Native Codex subagents require an explicit owner request" in startup
        assert "do not substitute native helpers, shell workers or execution in this main session" in startup
        assert "No periodic status checks, empty native waits" in startup
        assert "native subagents only for short" not in startup
        # Keep the explicitly requested exception available; do not disable tools,
        # change MCP authority, or silently grant durable dispatch to the old gate.
        assert configuration["agents"]["enabled"] is True
        assert (configuration["model"], configuration["model_reasoning_effort"]) == ("gpt-6-luna", "high")
        skill = selected.files[".agents/skills/bazarlyk-role-operations-delegation/SKILL.md"].decode()
        assert "Task size never selects a native subagent" in skill
        assert "No periodic status reads" in skill
        assert "a native `gpt-6-luna` helper for one short" not in skill
