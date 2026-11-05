"""Core preparation of the v3 Email Management instruction package, with synthetic fixtures only.

Covers the package contract, the Ordubashy-only default, the one-time instance seeding that keeps local edits, the resolved
native discovery skill and the refusal of a symlinked instance directory. No provider, network or real package is used."""

from dataclasses import replace
import json
from pathlib import Path
import tomllib

import pytest

from bazarlyk.agents import plugin_defaults
from bazarlyk.bootstrap import email_guidance
from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.catalog.packages import files_digest
from bazarlyk.instance_paths import InstancePathError
from qorqut_operations.contracts import DomainError, Principal
from email_guidance_support import (PLACEHOLDER, configure_sources, create_agent, grants, owner, publish_plugin,
                                    synthetic_ordubashy, write_package)
from host_foundation_support import WORKER_ROLE, build_instance, register_sources

VERSIONS = {"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.156.1"}
SKILL = "bazarlyk-plugin-email-management"


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("QORQUT_INSTALLATION_CONFIG", raising=False)
    from bazarlyk.bootstrap import diagnostics
    monkeypatch.setattr(diagnostics, "prepared_helpers", lambda *_: {"ready": True})
    package = write_package(tmp_path / "email-management-v3")
    configure_sources(monkeypatch, tmp_path, package)
    instance, _ = build_instance(tmp_path)
    register_sources(instance, instance.business, "first")
    synthetic_ordubashy(instance)
    instance.pin = publish_plugin(instance)
    return instance


def prepare(instance, agent):
    return prepare_agent(instance.runtime.resolver, Principal(agent, "agent"), endpoint="http://127.0.0.1:9988", versions=VERSIONS)


def package_of(instance):
    return instance.runtime.resolver.catalog.get("plugin", instance.pin)


def edited(package, **files):
    """The same package with some files replaced (None removes one); its digest follows its bytes, like a real publication."""
    changed = {**package.files, **{name: body for name, body in files.items()}}
    changed = {name: body for name, body in changed.items() if body is not None}
    return replace(package, files=changed, digest=files_digest(changed))


# -- the package contract --------------------------------------------------------------------------------------------

def test_the_v3_contract_is_accepted(host):
    email_guidance.validate_package(package_of(host))


@pytest.mark.parametrize("change", [
    lambda p: replace(p, manifest={**p.manifest, "version": "2.4.0"}),
    lambda p: edited(p, **{"tools.yaml": b"tools:\n  mail:\n    tool: jobs_get\n"}),
    lambda p: edited(p, **{"rules.yaml": b"channel: email\napproval: signoff\n"}),
    lambda p: edited(p, **{"skills/email-management/SKILL.md": b"---\nname: x\n---\nNo placeholder.\n"}),
    lambda p: edited(p, **{"skills/email-management/instance-template/extra.md": b"# extra\n"}),
    lambda p: edited(p, **{"skills/email-management/instance-template/providers/fastmail.md": None}),
    lambda p: edited(p, **{"skills/email-management/instance-template/instructions.md": b"{{OTHER}} x\n"}),
])
def test_anything_else_is_refused(host, change):
    with pytest.raises(DomainError) as refused:
        email_guidance.validate_package(change(package_of(host)))
    assert refused.value.code == "invalid_artifact"


# -- defaults and grants ---------------------------------------------------------------------------------------------

def test_only_ordubashy_receives_the_package_by_default():
    ecosystem, business = {"kind": "ecosystem"}, {"kind": "business"}
    assert plugin_defaults.default_plugin_ids("ordubashy", ecosystem) == (plugin_defaults.EMAIL_PLUGIN,)
    for role in ("global-worker", "learning-applier", "business-agent", "anything-else"):
        assert plugin_defaults.default_plugin_ids(role, ecosystem) == ()
    assert plugin_defaults.default_plugin_ids("ordubashy", business) == ()
    assert plugin_defaults.default_config(plugin_defaults.EMAIL_PLUGIN) == {}
    assert not hasattr(plugin_defaults, "sending_mailboxes")


def test_defaults_pin_the_published_v3_and_workers_get_it_only_by_explicit_grant(host):
    ordubashy = create_agent(host, "ordubashy", "ordubashy")
    assert grants(host, ordubashy) == [{**host.pin, "config": {}}]
    worker = create_agent(host, WORKER_ROLE, "scribe")
    assert grants(host, worker) == []
    explicit = create_agent(host, WORKER_ROLE, "mailer", plugins=[{**host.pin, "config": {}}])
    assert grants(host, explicit) == [{**host.pin, "config": {}}]
    with_mailboxes = host.invoke(owner(host), "agents_create", {
        "role": host.role_pins[WORKER_ROLE], "name": "legacy", "display_name": "Legacy", "assignment_brief": "Synthetic.",
        "plugins": [{**host.pin, "config": {"mailboxes": []}}],
        "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}})
    assert with_mailboxes.status != "ok"    # no mailbox configuration exists in v3


# -- seeding and the resolved native skill --------------------------------------------------------------------------

def seeded_files(home: Path):
    return sorted(str(item.relative_to(home)) for item in home.rglob("*") if item.is_file())


def test_preparation_seeds_the_instance_once_and_resolves_the_native_skill(host):
    agent = create_agent(host, "ordubashy", "ordubashy")
    home = host.paths.operations / "extensions" / "email-management"
    assert not home.exists()
    first = prepare(host, agent)
    assert home == host.paths.extension_home("email-management")
    assert first["ready"] and first["projection_drift"] == []
    assert first["instance_guidance"] == {"email-management": {"path": str(home), "seeded": True}}
    assert seeded_files(home) == [".origin.json", "SKILL.md", "inboxes/README.md", "instructions.md",
                                  "providers/fastmail.md", "providers/gmail.md"]
    origin = json.loads((home / ".origin.json").read_text())
    assert origin["package"] == host.pin and set(origin["files"]) == set(email_guidance.TEMPLATE_FILES)
    workspace = Path(first["workspace"])
    for root in (".agents/skills", ".claude/skills"):
        generated = workspace / root / SKILL
        text = (generated / "SKILL.md").read_text()
        assert str(home) in text and PLACEHOLDER not in text
        assert sorted(item.name for item in generated.iterdir()) == ["SKILL.md"]     # a discovery entry, not the template
    assert PLACEHOLDER.encode() not in b"".join(p.read_bytes() for p in workspace.rglob("SKILL.md"))


def test_local_edits_survive_reparation_and_nothing_is_reseeded(host):
    agent = create_agent(host, "ordubashy", "ordubashy")
    prepare(host, agent)
    home = host.paths.extension_home("email-management")
    (home / "instructions.md").write_text("Owner rule: never reply before 9.\n")
    (home / "providers/gmail.md").unlink()
    (home / "inboxes/work.md").write_text("Work inbox: owner@example.test\n")
    origin = (home / ".origin.json").read_bytes()
    second = prepare(host, agent)
    assert second["instance_guidance"]["email-management"]["seeded"] is False
    assert (home / "instructions.md").read_text() == "Owner rule: never reply before 9.\n"
    assert not (home / "providers/gmail.md").exists()          # a deliberate deletion is not undone
    assert (home / "inboxes/work.md").read_text() == "Work inbox: owner@example.test\n"
    assert (home / ".origin.json").read_bytes() == origin
    assert not [p for p in home.parent.iterdir() if "seeding" in p.name]


def test_workers_share_the_one_instance_directory_and_others_get_no_package(host):
    ordubashy = create_agent(host, "ordubashy", "ordubashy")
    scribe = create_agent(host, WORKER_ROLE, "scribe")
    mailer = create_agent(host, WORKER_ROLE, "mailer", plugins=[{**host.pin, "config": {}}])
    prepare(host, ordubashy)
    home = host.paths.extension_home("email-management")
    (home / "instructions.md").write_text("Shared edit.\n")
    plain = prepare(host, scribe)
    assert plain["instance_guidance"] == {} and not (Path(plain["workspace"]) / ".agents/skills" / SKILL).exists()
    shared = prepare(host, mailer)
    assert shared["instance_guidance"]["email-management"]["seeded"] is False
    assert (Path(shared["workspace"]) / ".agents/skills" / SKILL / "SKILL.md").read_text().count(str(home)) >= 1
    assert (home / "instructions.md").read_text() == "Shared edit.\n"


def test_a_symlinked_instance_directory_is_refused_before_anything_is_written(host, tmp_path):
    agent = create_agent(host, "ordubashy", "ordubashy")
    outside = tmp_path / "outside"
    outside.mkdir()
    (host.paths.operations / "extensions").symlink_to(outside)
    with pytest.raises((InstancePathError, DomainError)):
        prepare(host, agent)
    assert list(outside.iterdir()) == []
    (host.paths.operations / "extensions").unlink()
    (host.paths.operations / "extensions").mkdir()
    (host.paths.operations / "extensions/email-management").symlink_to(outside)
    with pytest.raises((InstancePathError, DomainError)):
        prepare(host, agent)
    assert list(outside.iterdir()) == []


def test_a_file_in_place_of_the_instance_directory_is_a_conflict(host):
    agent = create_agent(host, "ordubashy", "ordubashy")
    target = host.paths.operations / "extensions" / "email-management"
    target.parent.mkdir(parents=True)
    target.write_text("not a directory")
    with pytest.raises(DomainError) as refused:
        prepare(host, agent)
    assert refused.value.code == "projection_conflict"
    assert target.read_text() == "not a directory"


# -- native connectors -----------------------------------------------------------------------------------------------

def test_existing_native_connectors_survive_and_are_not_claimed(host):
    agent = create_agent(host, "ordubashy", "ordubashy")
    first = prepare(host, agent)
    workspace = Path(first["workspace"])
    mcp = json.loads((workspace / ".mcp.json").read_text())
    mcp["mcpServers"]["owner-gmail"] = {"type": "http", "url": "http://127.0.0.1:9/gmail"}
    (workspace / ".mcp.json").write_text(json.dumps(mcp))
    codex = workspace / ".codex/config.toml"
    codex.write_text(codex.read_text() + '\n[mcp_servers.owner-gmail]\nurl = "http://127.0.0.1:9/gmail"\n')
    second = prepare(host, agent)
    assert "owner-gmail" in json.loads((workspace / ".mcp.json").read_text())["mcpServers"]
    assert "owner-gmail" in tomllib.loads(codex.read_text())["mcp_servers"]
    assert "owner-gmail" not in second["connection_status"]        # preparation claims only the connections it manages
    assert "owner-gmail" not in (workspace / ".agents/skills" / SKILL / "SKILL.md").read_text()
