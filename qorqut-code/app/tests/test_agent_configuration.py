from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace
from dataclasses import replace
from pathlib import Path
import shutil
import shlex
import sqlite3
import subprocess
import sys

import pytest
import yaml

from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS, markdown_section
from bazarlyk.agents.models import AgentProfile
from bazarlyk.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from bazarlyk.agents.operations import AgentOperations
from bazarlyk.agents.resolver import AgentConfigResolver
from bazarlyk.application.catalog import builtin_registry
from bazarlyk.application.engine import Application
from bazarlyk.application.registry import OperationDescriptor
from bazarlyk.bootstrap.claude_session import run
from bazarlyk.bootstrap.client import auth_headers, load_registration
from bazarlyk.bootstrap.preparation import compatibility_report, prepare_agent, prepare_claude_agent
from bazarlyk.catalog.operations import CatalogOperations
from bazarlyk.catalog.packages import Catalog, content_hash, load_package
from bazarlyk.catalog.sources import load_sources
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id
from bazarlyk.storage import Database
from bazarlyk.storage.files import FileApplier
from bazarlyk.storage.receipts import ApplyReceipts
from bazarlyk.policy import PolicyResolver
from qorqut_operations.signoffs import SignoffService
from qorqut_operations.storage.executor import PreparedFileExecutor
from qorqut_operations.storage.structured import parse_yaml
from instance_support import PATHS, workspace_root
from schema_seed import initialized

REPO = workspace_root()


def role_ready_registry():
    """A builtin registry with the operations build_runtime registers dynamically, which role manifests name."""
    from bazarlyk.browser.operations import register_browser_operation
    registry = builtin_registry()
    register_browser_operation(SimpleNamespace(registry=registry))
    OperationalDelegationOperations(OperationalDelegation()).register(registry, None)
    return registry


REDDIT_CONFIG = {"subreddits": ["ClaudeAI", "selfhosted"], "cadence": "weekly", "link_policy": "comment", "account_ref": "reddit-founder"}


class Fixture:
    def __init__(self, temporary, *, real_initialization=False):
        self.root = temporary / "businesses"
        self.root.mkdir()
        self.hq = self.root / "widget/hq"
        self.hq.mkdir(parents=True)
        self.db = initialized(Database.for_root(self.root), real=real_initialization)
        self.registry = builtin_registry()
        # Role manifests are validated before fixture application assembly.
        # Include the same dynamically registered operations as build_runtime.
        from bazarlyk.browser.operations import register_browser_operation
        register_browser_operation(self)
        self.delegation = OperationalDelegation()
        self.delegation_operations = OperationalDelegationOperations(self.delegation).register(self.registry, None)
        self.owner = Principal(new_id(), "human", "Owner")
        self.business_id = new_id()
        with self.db.transaction() as uow:
            uow.repository("principals").insert({"id": self.owner.id, "kind": "human", "display_name": "Owner"})
            uow.repository("businesses").insert({"id": self.business_id, "slug": "widget", "workspace": str(self.hq.parent), "hq_path": str(self.hq), "name": "Widget", "stage": "validating"})
            uow.repository("business_memberships", business_id=self.business_id).insert({"principal_id": self.owner.id, "is_owner": True})
        self.ctx = InvocationContext(self.owner, business_id=self.business_id, allowed_business_ids=frozenset({self.business_id}),
            global_capabilities=frozenset({"read_global_context", "manage_shared_instructions", "publish_roles", "publish_plugins", "manage_ecosystem_agents"}), source="dashboard", review_business_id=self.business_id)
        self.catalog = Catalog(self.db, self.root, self.registry)
        self.sources = load_sources(REPO, paths=PATHS)
        # Paired repository worktrees must test their companion role artifacts,
        # even when template/plugin fixtures come from the shared workspace.
        # Cross-repository integration runs may pair this core checkout with a dedicated
        # agent-roles worktree. Keep the default sibling checkout, but make the pairing
        # explicit so a newer role manifest cannot silently collide with this core registry.
        role_source = Path(os.environ.get("QORQUT_TEST_AGENT_ROLES_ROOT",
                                        PATHS.component("roles")))
        if role_source.is_dir():
            for manifest in (role_source / "roles").glob("*/role.yaml"):
                self.sources[manifest.parent.name] = ("role", role_source, manifest.relative_to(role_source).as_posix())
        self.publisher = CatalogOperations(self.catalog, sources=self.sources)
        self.refs = {}
        with self.db.transaction() as uow:
            for name, (kind, source, manifest) in self.sources.items():
                package = load_package(source, manifest, kind, self.registry)
                destination = self.catalog.artifact_path(package)
                for relative, content in package.files.items():
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                uow.repository("artifacts").insert({"kind": kind, "artifact_id": package.id, "version": package.version,
                    "digest": package.digest, "stored_path": str(destination), "metadata_json": {"manifest_path": manifest}})
                self.refs[name] = package.ref
        from bazarlyk.bootstrap.seed import INSTRUCTION_SEEDS, instruction_seed
        for key, path in INSTRUCTION_SEEDS:
            self.document(key, instruction_seed(path).decode(), "files/instructions/" + path)
        for key in ("product", "icp", "brand"):
            self.document(key, f"# {key.title()}\n\n## Agent Brief\nSynthetic fixture {key} document.\n", f"context/{key}.md", self.business_id)

        def unavailable(*args):
            raise DomainError("not_exercised", "Plugin execution is not part of the profile fixture")
        self.registry.register(OperationDescriptor("signoffs_platform_submit", {"type": "object"},
            prepare=lambda *args: None, apply=lambda *args: {}))
        self.registry.register(OperationDescriptor("fixture_plugin_action", {"type": "object"},
            aliases=("fixture_reddit_action",), plugin_aliases={"fixture_reddit_action": "reddit-organic"}, prepare=unavailable, apply=unavailable))
        self.resolver = AgentConfigResolver(self.db, self.root, self.registry, plugin_handlers={"reddit-organic": self.validate_reddit})
        self.ops = AgentOperations(self.resolver)
        self.ops.register(self.registry)
        self.publisher.register(self.registry)

    def historical_role(self, role_id):
        """Register an immutable historical artifact for a retired role whose source was removed."""
        source = self.root.parent / "historical-roles"
        folder = source / "roles" / role_id
        folder.mkdir(parents=True, exist_ok=True)
        template = self.sources["coder"][1] / "roles/coder"
        (folder / "BRIEF.md").write_text((template / "BRIEF.md").read_text())
        (folder / "role.yaml").write_text((template / "role.yaml").read_text()
            .replace("id: coder", f"id: {role_id}", 1).replace("roles/coder/BRIEF.md", f"roles/{role_id}/BRIEF.md"))
        package = load_package(source, f"roles/{role_id}/role.yaml", "role", self.registry)
        destination = self.catalog.artifact_path(package)
        for relative, content in package.files.items():
            (destination / relative).parent.mkdir(parents=True, exist_ok=True)
            (destination / relative).write_bytes(content)
        with self.db.transaction() as uow:
            uow.repository("artifacts").insert({"kind": "role", "artifact_id": role_id, "version": package.version,
                "digest": package.digest, "stored_path": str(destination), "metadata_json": {"manifest_path": f"roles/{role_id}/role.yaml"}})
        self.refs[role_id] = package.ref
        return package.ref

    @staticmethod
    def validate_reddit(package):
        definitions = parse_yaml(package.files[package.manifest["tools"]].decode())
        if {item["tool"] for item in definitions["tools"].values()} != {"reddit.post", "reddit.comment", "reddit.read"}:
            raise DomainError("missing_plugin_handler", "Fixture recognizes only the agreed Reddit operations")

    def publish(self, source_id):
        kind = self.sources[source_id][0]
        with self.db.read() as uow:
            plan = self.publisher.prepare(self.ctx, {"source_id": source_id}, InvocationOptions(idempotency_key=new_id()), uow, kind)
        self.apply_files(plan, self.publisher.resolve_file_target)
        with self.db.transaction() as uow:
            return self.publisher.apply(self.ctx, plan, uow)["artifact"]

    def apply_files(self, plan, resolver):
        with self.db.transaction() as uow:
            receipt = ApplyReceipts(uow).create(plan)
        FileApplier(self.db, resolver).apply(receipt["id"])
        with self.db.transaction() as uow:
            ApplyReceipts(uow).finish(receipt["id"], {"fixture": True})

    def document(self, key, text, relative, business_id=None):
        path = (self.hq if business_id else self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        with self.db.transaction() as uow:
            return uow.repository("documents", business_id=business_id).insert({"id": new_id(), "namespace": business_id or "global",
                "kind": "context" if business_id else "shared_instruction", "path": relative, "content_hash": content_hash(text.encode()), "metadata_json": {"bootstrap_key": key}})

    def create(self, name=None, role="business-agent", reddit=False, **extra):
        """The business's Onbashy by default (or its Dev peer); Global roles need a name."""
        if role in ("business-agent", "dev"):
            assert name is None, "A business role's name and display name are fixed"
            arguments = {"role": self.refs[role], **extra}
        else:
            arguments = {"name": name, "display_name": name.title(), "role": self.refs[role],
                         "assignment_brief": "Own measurable progress with current evidence.", **extra}
        if reddit:
            arguments["plugins"] = [{**self.refs["reddit-organic"], "config": dict(REDDIT_CONFIG)}]
        if role == "ordubashy" and "scope" not in extra:
            arguments["scope"] = {"kind": "ecosystem", "allowed_business_ids": [self.business_id], "global_capabilities": sorted(self.ctx.global_capabilities)}
        if role in {"founder", "global-marketing"} and "scope" not in extra:
            arguments["scope"] = {"kind": "ecosystem", "allowed_business_ids": [self.business_id], "global_capabilities": ["read_global_context"]}
        with self.db.read() as uow:
            plan = self.ops.prepare("agents_create", self.ctx, arguments, InvocationOptions(idempotency_key=new_id()), uow)
        self.apply_files(plan, self.ops.resolve_file_target)
        with self.db.transaction() as uow:
            result = self.ops.apply(self.ctx, plan, uow)
        return Principal(result["agent_id"], "agent", plan.changes[0].values["profile"]["display_name"])

    def enable_reddit(self, principal):
        """Add the Reddit plugin to an existing agent, keeping its other plugins."""
        profile, _ = self.resolver.profile(principal)
        plugins = [plugin for plugin in profile.data["plugins"] if plugin["id"] != "reddit-organic"]
        return self.update(principal, {"plugins": [*plugins, {**self.refs["reddit-organic"], "config": dict(REDDIT_CONFIG)}]})

    def update(self, principal, changes):
        profile, _ = self.resolver.profile(principal)
        with self.db.read() as uow:
            plan = self.ops.prepare("agents_update", self.ctx, {"agent_id": principal.id, "changes": changes}, InvocationOptions(expected_revision=profile.revision, idempotency_key=new_id()), uow)
        with self.db.transaction() as uow:
            self.ops.apply(self.ctx, plan, uow)
        return plan

    def prepare(self, principal):
        return prepare_claude_agent(self.resolver, principal, endpoint="http://127.0.0.1:9988", claude_version="2.1.263 (Claude Code)")

    def application(self):
        policy = PolicyResolver(self.registry, self.resolver)
        application = Application(self.db, self.registry, policy=policy, context_resolver=policy.refresh_context)
        application.operational_delegation = self.delegation
        self.delegation_operations.application = application
        self.ops.register_targets(application)
        application.register_target_resolver("artifacts", self.publisher.resolve_record)
        application.register_target_resolver("artifact_files", self.publisher.resolve_file_snapshot)
        def file_target(resource):
            return (self.publisher if resource.resource_type == "artifact_files" else self.ops).resolve_file_target(resource)
        applier = FileApplier(self.db, file_target)
        application.file_executor = PreparedFileExecutor(self.db, applier)
        return application, SignoffService(application, file_applier=applier).register_operations()


@pytest.fixture
def fixture(tmp_path):
    return Fixture(tmp_path)


def test_owner_can_adopt_legacy_ordubashy_runtime_without_changing_identity(fixture):
    principal = fixture.create("ordubashy", "ordubashy")
    # Existing installations predate the Codex-only Ordubashy requirement.
    with fixture.db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(),
                                               "owner_principal_id": fixture.owner.id})
        row = uow.repository("agent_profiles", business_id=None).get(principal.id)
        legacy = row["profile_json"]
        legacy["runtime"] = "claude-code"
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
                               (json.dumps(legacy), principal.id))
    app, _ = fixture.application()
    owner = replace(fixture.ctx, business_id=None, review_business_id=None)
    args = {"agent_id": principal.id, "changes": {"runtime": "codex"}}
    stale = app.invoke(owner, "agents_update", args,
                       InvocationOptions(expected_revision=legacy["profile_revision"] + 1, idempotency_key=new_id()))
    assert stale.status == "error" and stale.error["code"] == "revision_conflict"
    result = app.invoke(owner, "agents_update", args,
                        InvocationOptions(expected_revision=legacy["profile_revision"], idempotency_key=new_id()))
    assert result.status == "ok", result.to_dict()
    profile, _ = fixture.resolver.profile(principal)
    current = profile.to_dict()
    assert current["runtime"] == "codex"
    assert current["profile_revision"] == legacy["profile_revision"] + 1
    for key in ("agent_id", "name", "scope", "role", "assignment_brief", "plugins", "tool_policy"):
        assert current[key] == legacy[key]
    rejected = app.invoke(owner, "agents_update", {**args, "changes": {"runtime": "claude-code"}},
                          InvocationOptions(expected_revision=profile.revision, idempotency_key=new_id()))
    assert rejected.status == "error" and rejected.error["code"] == "invalid_profile"


@pytest.mark.parametrize("explicit", [False, True])
def test_preparation_uses_runtime_python_outside_release(fixture, tmp_path, monkeypatch, explicit):
    from bazarlyk.bootstrap import preparation

    release = tmp_path / "release without environment"
    module = release / "bazarlyk/bootstrap/preparation.py"
    module.parent.mkdir(parents=True)
    module.touch()
    for resource in ('activity_hook.py', 'technical_reporting.py'):
        shutil.copyfile(Path(preparation.__file__).with_name(resource), module.with_name(resource))
    monkeypatch.setattr(preparation, "__file__", str(module))
    assert not (release / ".venv").exists()
    interpreter = sys.executable
    arguments = {}
    if explicit:
        # A trusted override must take precedence over the default interpreter.
        arguments["interpreter"] = interpreter
        monkeypatch.setattr(preparation.sys, "executable", str(tmp_path / "missing-default-python"))
    principal = fixture.create()
    result = prepare_claude_agent(fixture.resolver, principal,
        endpoint="http://127.0.0.1:9988", claude_version="2.1.263 (Claude Code)", **arguments)
    assert result["ready"]
    workspace = Path(result["workspace"])
    settings = json.loads((workspace / ".claude/settings.local.json").read_text())
    commands = [hook["command"] for groups in settings["hooks"].values()
                for group in groups for hook in group["hooks"]]
    commands.append((workspace / ".bazarlyk/mcp-bridge").read_text().splitlines()[1].removeprefix("exec "))
    for command in commands:
        assert shlex.split(command)[:2] == [interpreter, "-I"]
    subprocess.run([interpreter, "-I", "-c",
        "import bazarlyk.bootstrap.claude_session, bazarlyk.bootstrap.mcp_headers, "
        "bazarlyk.bootstrap.transcript_snapshot"], check=True, capture_output=True, timeout=15)


def test_preparation_rejects_missing_explicit_python(fixture, tmp_path):
    principal = fixture.create()
    with pytest.raises(DomainError, match="interpreter is unavailable"):
        prepare_claude_agent(fixture.resolver, principal,
            endpoint="http://127.0.0.1:9988", claude_version="2.1.263 (Claude Code)",
            interpreter=str(tmp_path / "missing-python"))


def test_preparation_accepts_container_interpreter_with_trusted_validator(fixture, tmp_path):
    """The real projection accepts a path that exists only in the target container."""
    principal = fixture.create()
    container_python = "/opt/qorqut/venv/bin/python"
    assert not Path(container_python).is_file()
    checked = []

    def validate(path):
        checked.append(path)
        return path == container_python

    result = prepare_agent(fixture.resolver, principal, endpoint="http://127.0.0.1:9988",
        versions={"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.153.4"},
        interpreter=container_python, interpreter_validator=validate)
    assert result["ready"]
    assert checked == [container_python]
    assert container_python in (Path(result["workspace"]) / ".claude/settings.local.json").read_text()


def test_business_agent_skills_follow_its_plugin_grants(fixture):
    agent = fixture.create(plugins=[])
    first = fixture.resolver.resolve_agent_config(agent).to_dict()
    assert first["role"]["id"] == "business-agent" and first["name"] == "business-agent"
    skills = {row["name"] for row in first["skills"]}
    assert {"role", "bazarlyk-role-campaign-execution", "bazarlyk-role-business-testing", "bazarlyk-role-business-tables"} <= skills
    assert "bazarlyk-role-marketing-plan" not in skills
    assert "bazarlyk-plugin-reddit-organic" not in skills
    assert agent.id in first["mandatory_context"]["rendered"]
    assert "Granted optional plugins: none" in first["mandatory_context"]["rendered"]
    assert first["mandatory_context"]["character_count"] <= MAX_CONTEXT_CHARACTERS
    assert {entry["section_selector"] for entry in first["mandatory_context"]["entries"]} >= {"Agent Brief"}
    ready = fixture.prepare(agent)
    path = Path(ready["workspace"])
    assert ready["ready"] and path == fixture.hq / "agents/business-agent" and (path / "PLAN.md").is_file()
    assert (path / ".claude/skills/bazarlyk-role-business-testing/references/campaign.md").is_file()
    assert not (path / ".claude/skills/bazarlyk-plugin-reddit-organic").exists()
    fixture.update(agent, {"plugins": [{**fixture.refs["reddit-organic"], "config": {"subreddits": ["ClaudeAI"], "cadence": "weekly",
        "link_policy": "comment", "account_ref": "reddit-founder"}}]})
    second = fixture.resolver.resolve_agent_config(agent).to_dict()
    assert "bazarlyk-plugin-reddit-organic" in {row["name"] for row in second["skills"]}
    assert fixture.prepare(agent)["ready"] and (path / ".claude/skills/bazarlyk-plugin-reddit-organic").exists()


def test_business_agent_prepares_with_bounded_permissions(fixture):
    agent = fixture.create()
    config = fixture.resolver.resolve_agent_config(agent).to_dict()
    assert config["business_id"] == fixture.business_id
    assert config["role"]["id"] == "business-agent"
    modes = {tool["canonical_operation"]: tool["effective_mode"] for tool in config["tools"]}
    for name in ("plans_set", "notes_create", "tasks_create", "stories_update", "context_get", "tables_rows_write", "ledger_add"):
        assert modes[name] == "allow"
    for name in ("agents_create", "agents_update", "agents_deactivate", "context_document_update",
                 "fixture_plugin_action", "roles_publish", "plugins_publish", "shared_instructions_update"):
        assert modes[name] == "deny"
    for name in ("business_config_update", "permissions_set", "routines_run", "learnings_extract"):
        assert modes[name] == "signoff"
    ready = fixture.prepare(agent)
    assert ready["ready"]
    assert (Path(ready["workspace"]) / "PLAN.md").is_file()
    with pytest.raises(DomainError):
        fixture.resolver.resolve_agent_config(agent, business_id=new_id())


def test_business_agent_and_root_receive_correct_scope_context(fixture):
    agent = fixture.create()
    root = fixture.create("ordubashy", "ordubashy")
    agent_config = fixture.resolver.resolve_agent_config(agent).to_dict()
    root_config = fixture.resolver.resolve_agent_config(root).to_dict()
    assert "Business Scope" in agent_config["mandatory_context"]["rendered"]
    assert "Business Scope" not in root_config["mandatory_context"]["rendered"]
    assert "Ecosystem Scope" in root_config["mandatory_context"]["rendered"]
    assert root_config["business_id"] is None
    assert root_config["workspace"] == str(fixture.root / "agents/ordubashy")
    assert not (fixture.root / "business.yaml").exists()
    selected = fixture.resolver.resolve_agent_config(root, business_id=fixture.business_id).to_dict()
    assert "Core Product" not in selected["mandatory_context"]["rendered"]
    assert "Ecosystem Scope" in selected["mandatory_context"]["rendered"]
    modes = {tool["canonical_operation"]: tool["effective_mode"] for tool in agent_config["tools"]}
    assert modes["agents_create"] == "deny"
    assert modes["roles_publish"] == "deny"
    assert fixture.prepare(root)["ready"]


@pytest.mark.parametrize("changes", [
    {"runtime": "deepseek"}, {"kind": "marketing"}, {"role": {"id": "marketing"}},
    {"tool_policy": {"overrides": {"unknown_write": "allow"}}},
    {"tool_policy": {"overrides": {"tasks_list": "signoff"}}},
    {"tool_policy": {"overrides": {"signoffs_review": "allow"}}},
])
def test_invalid_profile_is_rejected(fixture, changes):
    principal = fixture.create()
    profile, _ = fixture.resolver.profile(principal)
    with pytest.raises(DomainError):
        AgentProfile.from_dict({**profile.to_dict(), **changes}, fixture.registry)


def test_retired_roles_cannot_be_created_selected_listed_or_run(fixture):
    with fixture.db.read() as uow:
        plan = fixture.ops.prepare("agents_create", fixture.ctx, {"role": fixture.refs["business-agent"]},
            InvocationOptions(idempotency_key=new_id()), uow)
    for role in ("coder", "onbashy", "strategist", "marketing", "outreach-specialist"):
        if role not in fixture.refs:
            fixture.historical_role(role)
        with pytest.raises(DomainError, match="retired"):
            fixture.create(role, role)
    principal = fixture.create()
    for role in ("coder", "onbashy"):
        with pytest.raises(DomainError, match="retired"):
            fixture.update(principal, {"role": fixture.refs[role]})
    with pytest.raises(DomainError):
        fixture.update(principal, {"role": fixture.refs["global-marketing"]})
    first = plan.changes[0]
    values = dict(first.values)
    values["profile"] = {**values["profile"], "role": fixture.refs["coder"]}
    stale = replace(plan, changes=(replace(first, values=values), *plan.changes[1:]))
    with fixture.db.read() as uow:
        with pytest.raises(DomainError, match="retired"):
            fixture.ops.validate_targets(fixture.ctx, stale, uow)
        listed = fixture.ops.read("roles_list", fixture.ctx, {}, InvocationOptions(), uow)["roles"]
    assert {row["id"] for row in listed} >= {"business-agent", "global-marketing", "founder", "ordubashy"}
    assert not {row["id"] for row in listed} & {"coder", "onbashy", "strategist", "marketing", "outreach-specialist"}
    # A profile still pinned to a retired role fails closed.
    with fixture.db.transaction() as uow:
        row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (principal.id,)).fetchone()
        pinned = {**json.loads(row["profile_json"]), "role": fixture.refs["onbashy"]}
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(pinned), principal.id))
    with pytest.raises(DomainError) as error:
        fixture.resolver.profile(principal)
    assert error.value.code == "inactive_agent"


def test_coder_artifact_is_readable_but_hidden_from_selection_and_publication(fixture):
    application, _ = fixture.application()
    exact = application.invoke(fixture.ctx, "roles_get", {"role": fixture.refs["coder"]})
    assert exact.status == "ok" and exact.data["role"]["id"] == "coder"
    listed = application.invoke(fixture.ctx, "roles_list", {})
    assert listed.status == "ok"
    assert "coder" not in {item["id"] for item in listed.data["roles"]}
    with pytest.raises(DomainError, match="retired"):
        fixture.publish("coder")

    with fixture.db.read() as uow:
        valid = fixture.publisher.prepare(fixture.ctx, {"source_id": "global-marketing"},
            InvocationOptions(idempotency_key=new_id()), uow, "role")
    record = next(change for change in valid.changes if change.kind == "record")
    forged_values = dict(record.values)
    forged_values["artifact_id"] = "coder"
    stale = replace(valid, changes=(*valid.changes[:-1], replace(record, values=forged_values)))
    with fixture.db.read() as uow:
        with pytest.raises(DomainError, match="retired"):
            fixture.publisher.validate_targets(fixture.ctx, stale, uow)


def test_profile_export_cannot_grant_and_drift_is_repaired(fixture):
    principal = fixture.create()
    ready = fixture.prepare(principal)
    path = Path(ready["workspace"]) / "agent.yaml"
    value = yaml.safe_load(path.read_text())
    value["tool_policy"]["overrides"]["agents_create"] = "allow"
    path.write_text(yaml.safe_dump(value))
    config = fixture.resolver.resolve_agent_config(principal).to_dict()
    assert config["tool_policy"]["overrides"] == {}
    assert "agent.yaml" in config["preparation"]["projection_drift"]
    assert not config["preparation"]["ready"]
    assert fixture.prepare(principal)["ready"]


def test_unregistered_manual_skill_is_readiness_error(fixture):
    principal = fixture.create()
    ready = fixture.prepare(principal)
    path = Path(ready["workspace"]) / ".claude/skills/bazarlyk-plugin-ungranted"
    path.mkdir()
    (path / "SKILL.md").write_text("not granted")
    assert not fixture.resolver.resolve_agent_config(principal).to_dict()["preparation"]["ready"]
    with pytest.raises(DomainError, match="Unknown manually"):
        fixture.prepare(principal)


def test_credential_reissue_revokes_previous_and_project_symlinks_are_not_followed(fixture, tmp_path):
    principal = fixture.create()
    ready = fixture.prepare(principal)
    workspace = Path(ready["workspace"])
    with fixture.db.read() as uow:
        prior = uow.repository("machine_registrations").get(ready["registration_id"])["credential_id"]
    (workspace / ".bazarlyk/credential").unlink()
    assert "scoped_credential_unavailable" in fixture.resolver.resolve_agent_config(principal).data["preparation"]["projection_drift"]
    assert fixture.prepare(principal)["ready"]
    with fixture.db.read() as uow:
        assert uow.repository("credentials").get(prior)["revoked_at"]
    external = tmp_path / "outside-project"
    external.mkdir()
    (external / "settings.json").write_text('{"unrelated": true}\n')
    (workspace / ".claude").rename(workspace / "preserved-project-settings")
    (workspace / ".claude").symlink_to(external, target_is_directory=True)
    assert not fixture.resolver.resolve_agent_config(principal).data["preparation"]["ready"]
    with pytest.raises(DomainError) as error:
        fixture.prepare(principal)
    assert error.value.code == "projection_conflict"
    assert sorted(path.name for path in external.iterdir()) == ["settings.json"]


def test_plugin_revocation_removes_projection_and_blocks_old_bootstrap(fixture):
    principal = fixture.create(reddit=True)
    ready = fixture.prepare(principal)
    old_config = fixture.resolver.resolve_agent_config(principal)
    plan = fixture.update(principal, {"plugins": []})
    assert "plugins_disable" in {item.operation_name for item in plan.changes}
    current = fixture.resolver.resolve_agent_config(principal).to_dict()
    assert current["plugins"] == []
    assert not current["preparation"]["ready"]
    assert current["effective_config_id"] != old_config.effective_config_id
    repaired = fixture.prepare(principal)
    assert repaired["ready"]
    assert not (Path(ready["workspace"]) / ".claude/skills/bazarlyk-plugin-reddit-organic").exists()


def test_granted_plugin_requires_trusted_handler(fixture):
    principal = fixture.create(reddit=True)
    resolver = AgentConfigResolver(fixture.db, fixture.root, fixture.registry)
    with pytest.raises(DomainError) as error:
        resolver.resolve_agent_config(principal)
    assert error.value.code == "missing_plugin_handler"


def test_alias_discovery_and_call_use_same_registry_grant(fixture):
    agent = fixture.create()
    # Exercise alias policy independently of the any production operation.
    fixture.update(agent, {"tool_policy": {"overrides": {"fixture_plugin_action": "allow"}}})
    application, _ = fixture.application()
    reddit = [{**fixture.refs["reddit-organic"], "config": {"subreddits": ["ClaudeAI"], "cadence": "weekly",
        "link_policy": "comment", "account_ref": "reddit-founder"}}]
    for plugins, aliases, code in (([], [], "forbidden"), (reddit, ["fixture_reddit_action"], "not_exercised")):
        fixture.update(agent, {"plugins": plugins})
        config = fixture.resolver.resolve_agent_config(agent).to_dict()
        requests = next(row for row in config["tools"] if row["canonical_operation"] == "fixture_plugin_action")
        assert requests["exposed_aliases"] == aliases
        assert requests["plugin_requirement"] is None
        assert requests["effective_mode"] == "allow"
        result = application.invoke(fixture.resolver.invocation_context(agent), "fixture_reddit_action", {}, {"idempotency_key": new_id()})
        assert result.error["code"] == code


def test_owner_permissions_replace_replay_and_config_consistency(fixture):
    principal = fixture.create()
    application, _ = fixture.application()
    options = {"expected_revision": 1, "idempotency_key": new_id()}
    arguments = {"agent_id": principal.id, "overrides": {"agents_update": "signoff"}}
    first = application.invoke(fixture.ctx, "permissions_set", arguments, options)
    assert first.status == "ok", first.to_dict()
    assert first.data["profile_revision"] == 2
    replay = application.invoke(fixture.ctx, "permissions_set", arguments, options)
    assert replay.status == "ok", replay.to_dict()
    assert replay.data == first.data
    config = fixture.ops.owner_config(fixture.ctx, principal.id)
    assert config["profile_revision"] == 2
    assert config["effective_config_id"] == first.data["effective_config_id"]
    assert config["tool_policy"]["overrides"] == arguments["overrides"]
    cleared = application.invoke(fixture.ctx, "permissions_set", {"agent_id": principal.id, "overrides": {}},
        {"expected_revision": 2, "idempotency_key": new_id()})
    assert cleared.status == "ok", cleared.to_dict()
    assert fixture.resolver.profile(principal)[0].data["tool_policy"]["overrides"] == {}


def test_reviewed_business_agent_creation_reserves_its_name_and_blocks_a_second(fixture):
    coordinator = fixture.create("coordinator", "ordubashy")
    application, service = fixture.application()
    context = fixture.resolver.invocation_context(coordinator, business_id=fixture.business_id, review_business_id=fixture.business_id)
    arguments = {"role": fixture.refs["business-agent"]}
    for rejected in ({**arguments, "display_name": "Chosen"}, {**arguments, "name": "growth"}):
        assert application.invoke(fixture.ctx, "agents_create", rejected, {"idempotency_key": new_id()}).error["code"] == "validation_error"
    proposal = application.invoke(context, "agents_create", arguments, {"idempotency_key": new_id()})
    assert proposal.status == "signoff_required", proposal.to_dict()
    assert not (fixture.hq / "agents/business-agent").exists()
    reference = proposal.signoff
    accepted = service.review(fixture.ctx, reference["id"], decision="approve",
        proposal_revision=reference["proposal_revision"], payload_hash=reference["payload_hash"],
        expected_status_revision=reference["status_revision"], idempotency_key=new_id())
    assert accepted.status == "accepted", accepted.to_dict()
    service.run_one(reference["id"])
    assert service.get(fixture.ctx, reference["id"]).data["signoff"]["state"] == "applied"
    assert (fixture.hq / "agents/business-agent/PLAN.md").is_file()
    from bazarlyk.agents import roles
    with fixture.db.read() as uow:
        row = roles.require_business_agent(uow.connection, fixture.business_id)
        assert json.loads(row["profile_json"])["display_name"] == roles.BUSINESS_AGENT_DISPLAY_NAME
        assert uow.repository("principals").get(row["agent_id"])["display_name"] == roles.BUSINESS_AGENT_DISPLAY_NAME
        assert uow.repository("business_memberships", business_id=fixture.business_id).get(row["agent_id"])["active"]
    second = application.invoke(fixture.ctx, "agents_create", arguments, {"idempotency_key": new_id()})
    assert second.error["code"] == "roster_conflict"
    renamed = application.invoke(fixture.ctx, "agents_update", {"agent_id": row["agent_id"], "changes": {"display_name": "Chosen"}},
        {"expected_revision": row["revision"], "idempotency_key": new_id()})
    assert renamed.error["code"] == "invalid_profile"
    removed = application.invoke(fixture.ctx, "agents_deactivate", {"agent_id": row["agent_id"]},
        {"expected_revision": row["revision"], "idempotency_key": new_id()})
    assert removed.error["code"] == "roster_conflict"


@pytest.mark.parametrize("role", ["business-agent", "dev"])
def test_concurrent_business_role_creations_leave_exactly_one(fixture, role):
    from bazarlyk.agents import roles
    other = next(item for item in roles.BUSINESS_ROLES if item != role)
    plans = []
    for _ in range(2):
        with fixture.db.read() as uow:
            plans.append(fixture.ops.prepare("agents_create", fixture.ctx, {"role": fixture.refs[role]},
                                             InvocationOptions(idempotency_key=new_id()), uow))
    fixture.apply_files(plans[0], fixture.ops.resolve_file_target)
    with fixture.db.transaction() as uow:
        fixture.ops.apply(fixture.ctx, plans[0], uow)
    with fixture.db.transaction() as uow, pytest.raises(DomainError) as error:
        fixture.ops.apply(fixture.ctx, plans[1], uow)
    assert error.value.code == "roster_conflict"
    with fixture.db.read() as uow:
        assert len(roles.active_home_profiles(uow.connection, fixture.business_id)) == 1
        # Only the other peer is missing: no duplicate or foreign role.
        problem, = roles.roster_violations(uow.connection)
        assert problem["missing_roles"] == [other] and problem["duplicate_roles"] == problem["invalid_roles"] == []
        # The database refuses a second active profile of the role even below the application.
        row = dict(roles.business_role_agent(uow.connection, fixture.business_id, role))
    second = new_id()
    with fixture.db.transaction() as uow, pytest.raises(sqlite3.IntegrityError, match="one_active_role_per_home"):
        uow.connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES (?,'agent','Second','t','t')", (second,))
        uow.connection.execute("INSERT INTO agent_profiles(agent_id,principal_id,home_business_id,namespace,name,workspace,status,"
            "profile_json,revision,created_at,updated_at) SELECT ?,?,home_business_id,namespace,'second',workspace||'2',status,"
            "profile_json,revision,created_at,updated_at FROM agent_profiles WHERE agent_id=?", (second, second, row["agent_id"]))
    # The other peer's slot stays open.
    peer = fixture.create(role=other)
    with fixture.db.read() as uow:
        assert roles.business_role_agent(uow.connection, fixture.business_id, other)["agent_id"] == peer.id
        assert roles.roster_violations(uow.connection) == []


def test_dev_creation_uses_its_fixed_identity_home_and_no_default_plugins(fixture):
    from bazarlyk.agents import roles
    arguments = {"role": fixture.refs["dev"]}
    for rejected in ({**arguments, "display_name": "Engineer"}, {**arguments, "name": "coder"}):
        with fixture.db.read() as uow, pytest.raises(DomainError) as error:
            fixture.ops.prepare("agents_create", fixture.ctx, rejected, InvocationOptions(idempotency_key=new_id()), uow)
        assert error.value.code == "validation_error"
    dev = fixture.create(role="dev")
    profile = fixture.resolver.profile(dev)[0].data
    assert (profile["name"], profile["display_name"]) == (roles.DEV_NAME, roles.DEV_DISPLAY_NAME)
    assert list(profile["plugins"]) == [] and profile["runtime"] == "claude-code"
    assert profile["presentation"]["department"] == "engineering"
    assert profile["assignment_brief"] == roles.DEV_ASSIGNMENT
    with fixture.db.read() as uow:
        row = roles.require_dev_agent(uow.connection, fixture.business_id)
        assert uow.repository("business_memberships", business_id=fixture.business_id).get(dev.id)["active"]
    assert row["workspace"] == str(roles.dev_workspace(fixture.hq)) and row["name"] == roles.DEV_NAME
    # Without an installation owner nobody may disrupt Dev; the owner still cannot remove the last Dev.
    application = fixture.application()[0]
    arguments = ({"agent_id": dev.id}, {"expected_revision": row["revision"], "idempotency_key": new_id()})
    assert application.invoke(fixture.ctx, "agents_deactivate", *arguments).error["code"] == "forbidden"
    with fixture.db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": fixture.owner.id})
    removed = application.invoke(fixture.ctx, "agents_deactivate", {"agent_id": dev.id},
        {"expected_revision": row["revision"], "idempotency_key": new_id()})
    assert removed.error["code"] == "roster_conflict"


def test_dev_creation_refuses_an_occupied_home(fixture):
    from bazarlyk.agents import roles
    folder = roles.dev_workspace(fixture.hq)
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("owner work")
    with fixture.db.read() as uow, pytest.raises(DomainError):
        plan = fixture.ops.prepare("agents_create", fixture.ctx, {"role": fixture.refs["dev"]},
                                   InvocationOptions(idempotency_key=new_id()), uow)
        fixture.apply_files(plan, fixture.ops.resolve_file_target)
    assert (folder / "notes.md").read_text() == "owner work"
    with fixture.db.read() as uow:
        assert roles.dev_agent(uow.connection, fixture.business_id) is None


def test_business_agent_display_name_is_shared_and_fixed(fixture):
    from bazarlyk.agents import roles
    coordinator = fixture.create("coordinator", "ordubashy")
    application, service = fixture.application()
    context = fixture.resolver.invocation_context(coordinator, business_id=fixture.business_id, review_business_id=fixture.business_id)
    proposal = application.invoke(context, "agents_create", {"role": fixture.refs["business-agent"]}, {"idempotency_key": new_id()})
    reference = proposal.signoff
    # Display names are not unique: another principal already using the title does not block approval.
    with fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET display_name=? WHERE id=?", (roles.BUSINESS_AGENT_DISPLAY_NAME, coordinator.id))
    review = service.review(fixture.ctx, reference["id"], decision="approve", proposal_revision=reference["proposal_revision"],
        payload_hash=reference["payload_hash"], expected_status_revision=reference["status_revision"], idempotency_key=new_id())
    assert review.status == "accepted", review.to_dict()
    service.run_one(reference["id"])
    assert service.get(fixture.ctx, reference["id"]).data["signoff"]["state"] == "applied"
    with fixture.db.read() as uow:
        row = roles.require_business_agent(uow.connection, fixture.business_id)
    agent = Principal(row["agent_id"], "agent")
    assert fixture.resolver.profile(agent)[0].data["display_name"] == roles.BUSINESS_AGENT_DISPLAY_NAME
    # A drifted display name may be repaired back to the title, never changed to anything else.
    with fixture.db.transaction() as uow:
        profile = json.loads(uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (row["agent_id"],)).fetchone()[0])
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
                               (json.dumps({**profile, "display_name": "Drifted"}), row["agent_id"]))
    fixture.update(agent, {"display_name": roles.BUSINESS_AGENT_DISPLAY_NAME})
    assert fixture.resolver.profile(agent)[0].data["display_name"] == roles.BUSINESS_AGENT_DISPLAY_NAME


def test_manager_permission_signoff_applies_exact_profile_and_detects_source_drift(fixture):
    manager = fixture.create("manager", "ordubashy")
    target = fixture.create()
    application, service = fixture.application()
    context = fixture.resolver.invocation_context(manager, business_id=fixture.business_id, review_business_id=fixture.business_id)
    arguments = {"agent_id": target.id, "overrides": {"tasks_create": "signoff"}}
    proposal = application.invoke(context, "permissions_set", arguments,
        {"expected_revision": 1, "idempotency_key": new_id()})
    assert proposal.status == "signoff_required", proposal.to_dict()
    assert fixture.resolver.profile(target)[0].revision == 1
    reference = proposal.signoff
    accepted = service.review(fixture.ctx, reference["id"], decision="approve",
        proposal_revision=reference["proposal_revision"], payload_hash=reference["payload_hash"],
        expected_status_revision=reference["status_revision"], idempotency_key=new_id())
    assert accepted.status == "accepted", accepted.to_dict()
    service.run_one(reference["id"])
    assert fixture.resolver.profile(target)[0].revision == 2
    assert fixture.resolver.profile(target)[0].data["tool_policy"]["overrides"] == arguments["overrides"]
    second = application.invoke(context, "permissions_set", {"agent_id": target.id, "overrides": {}},
        {"expected_revision": 2, "idempotency_key": new_id()})
    assert second.status == "signoff_required", second.to_dict()
    reference = second.signoff
    accepted = service.review(fixture.ctx, reference["id"], decision="approve",
        proposal_revision=reference["proposal_revision"], payload_hash=reference["payload_hash"],
        expected_status_revision=reference["status_revision"], idempotency_key=new_id())
    assert accepted.status == "accepted", accepted.to_dict()
    source, _ = foundation_source(fixture)
    source.write_text(source.read_text() + "\nExternal edit after review.\n")
    service.run_one(reference["id"])
    assert fixture.resolver.profile(target)[0].revision == 2
    detail = service.get(fixture.ctx, reference["id"])
    assert detail.data["signoff"]["state"] in {"failed", "conflicted"}


def test_business_agent_cannot_route_root_edit_through_review_business(fixture):
    with fixture.db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": fixture.owner.id})
    manager = fixture.create()
    root = fixture.create("ordubashy", "ordubashy")
    application, _ = fixture.application()
    arguments = {"agent_id": root.id, "overrides": {"agents_update": "allow"}}
    options = {"expected_revision": 1, "idempotency_key": new_id(), "review_business_id": fixture.business_id}
    denied = application.invoke(fixture.resolver.invocation_context(manager), "permissions_set", arguments, options)
    assert denied.status == "error", denied.to_dict()
    assert denied.error["code"] == "forbidden"
    lacking_cap = application.invoke(replace(fixture.ctx, global_capabilities=frozenset({"read_global_context"})),
        "permissions_set", arguments, {**options, "idempotency_key": new_id()})
    assert lacking_cap.status == "error", lacking_cap.to_dict()
    assert lacking_cap.error["code"] == "forbidden"
    wrong_destination = application.invoke(fixture.ctx, "permissions_set", arguments, {**options, "idempotency_key": new_id()})
    assert wrong_destination.error["code"] == "validation_error"
    owner = application.invoke(fixture.ctx, "permissions_set", arguments, {"expected_revision": 1, "idempotency_key": new_id()})
    assert owner.status == "ok", owner.to_dict()
    assert fixture.resolver.profile(root)[0].revision == 2
    limited = application.invoke(fixture.ctx, "permissions_set", {"agent_id": root.id,
        "overrides": {"agents_update": "allow", "permissions_set": "deny"}},
        {"expected_revision": 2, "idempotency_key": new_id()})
    assert limited.status == "ok", limited.to_dict()
    profile, _ = fixture.resolver.profile(root)
    scope = profile.scope.to_dict()
    scope["global_capabilities"].remove("publish_plugins")
    scoped_change = application.invoke(fixture.resolver.invocation_context(root, review_business_id=fixture.business_id),
        "agents_update", {"agent_id": root.id, "changes": {"scope": scope}},
        {"expected_revision": 3, "idempotency_key": new_id()})
    assert scoped_change.error["code"] == "policy_denied", scoped_change.to_dict()
    assert fixture.resolver.profile(root)[0].revision == 3


def test_retired_archival_actor_is_visible_without_an_invented_role(fixture):
    actor = Principal(new_id(), "agent", "Old Code")
    with fixture.db.transaction() as uow:
        uow.repository("principals").insert({"id": actor.id, "kind": "agent", "display_name": actor.display_name, "active": False})
        uow.repository("agent_profiles", business_id=fixture.business_id).insert({"agent_id": actor.id, "principal_id": actor.id,
            "namespace": fixture.business_id, "name": "claude", "workspace": str(fixture.hq / "retired/claude"), "status": "retired",
            "profile_json": {"archival_record": True, "display_name": "Old Code", "legacy_actor_key": "agent:claude"}})
    application, _ = fixture.application()
    roster = application.invoke(fixture.ctx, "agents_list", {})
    assert roster.data["agents"] == ()
    archived = application.invoke(fixture.ctx, "agents_get", {"agent_id": actor.id})
    assert archived.status == "ok", archived.to_dict()
    assert archived.data["agent"]["archival_record"] is True
    assert archived.data["agent"]["status"] == "retired"
    assert "role" not in archived.data["agent"]
    all_agents = application.invoke(fixture.ctx, "agents_list", {"include_inactive": True})
    assert all_agents.data["agents"] == (archived.data["agent"],)
    permissions = application.invoke(fixture.ctx, "permissions_get", {"agent_id": actor.id})
    assert permissions.error["code"] == "inactive_agent"
    with pytest.raises(DomainError) as error:
        fixture.resolver.profile(actor)
    assert error.value.code == "inactive_agent"


def test_pinned_published_source_is_immutable(fixture, tmp_path):
    source = tmp_path / "source"
    shutil.copytree(fixture.sources["global-marketing"][1], source, ignore=shutil.ignore_patterns(".git"))
    fixture.sources["marketing-copy"] = ("role", source, "roles/global-marketing/role.yaml")
    fixture.publisher.sources["marketing-copy"] = fixture.sources["marketing-copy"]
    original = fixture.catalog.get("role", fixture.refs["global-marketing"])
    (source / "roles/global-marketing/BRIEF.md").write_text("Changed role responsibilities.")
    assert fixture.catalog.get("role", fixture.refs["global-marketing"]).digest == original.digest
    with fixture.db.read() as uow, pytest.raises(DomainError) as error:
        fixture.publisher.prepare(fixture.ctx, {"source_id": "marketing-copy"}, InvocationOptions(), uow, "role")
    assert error.value.code == "immutable_artifact"


@pytest.mark.parametrize("text", ["a: 1\na: 2\n", "true: value\n", "role: [\n"])
def test_yaml_parser_rejects_ambiguous_input(text):
    with pytest.raises(DomainError):
        parse_yaml(text)


@pytest.mark.parametrize("change", ["tool-grant", "shell", "symlink", "brief-budget", "unsafe-path"])
def test_package_validation_rejects_executable_or_unsafe_sources(fixture, tmp_path, change):
    source = tmp_path / "invalid-source"
    shutil.copytree(PATHS.component("roles"), source, ignore=shutil.ignore_patterns(".git"))
    skill = source / "skills/marketing-plan/SKILL.md"
    if change == "tool-grant":
        skill.write_text(skill.read_text().replace("description:", "allowed-tools: Bash\ndescription:", 1))
    elif change == "shell":
        skill.write_text(skill.read_text() + "\n!`rm file`\n")
    elif change == "symlink":
        (skill.parent / "escape.md").symlink_to(tmp_path / "external.md")
    elif change == "brief-budget":
        (source / "roles/global-marketing/BRIEF.md").write_text("x" * 10001)
    else:
        manifest = source / "roles/global-marketing/role.yaml"
        manifest.write_text(manifest.read_text().replace("brief: roles/global-marketing/BRIEF.md", "brief: ../outside.md"))
    with pytest.raises(DomainError):
        load_package(source, "roles/global-marketing/role.yaml", "role", fixture.registry)


@pytest.mark.parametrize("length", [0, 1, 4000, 4001, 10000, 10001])
def test_role_brief_character_boundary(tmp_path, length):
    source = tmp_path / "role-source"
    shutil.copytree(PATHS.component("roles"), source, ignore=shutil.ignore_patterns(".git"))
    # Unicode characters, not encoded bytes; surrounding whitespace is ignored.
    (source / "roles/global-marketing/BRIEF.md").write_text("\n" + "ğ" * length + "\n")
    if 1 <= length <= 10000:
        load_package(source, "roles/global-marketing/role.yaml", "role", builtin_registry())
    else:
        with pytest.raises(DomainError, match="1 to 10000 characters"):
            load_package(source, "roles/global-marketing/role.yaml", "role", builtin_registry())


def test_markdown_section_uses_structure():
    text = "# Product\n```md\n## Agent Brief\nwrong\n```\n## Agent Brief\nreal\n### Nested\nfacts\n## Details\nomit\n"
    assert markdown_section(text) == "real\n### Nested\nfacts"
    assert markdown_section("Agent Brief\n-----------\nreal\n# Other\nomit") == "real"
    with pytest.raises(DomainError):
        markdown_section("## Agent Brief\na\n## Agent Brief\nb")


def foundation_source(fixture):
    """The registered global Universal Foundation: a mandatory source (the authored pillars no longer are one)."""
    with fixture.db.read() as uow:
        row = next(row for row in uow.connection.execute("SELECT * FROM documents WHERE business_id IS NULL")
                   if json.loads(row["metadata_json"]).get("bootstrap_key") == "foundation")
    return fixture.resolver.root / row["path"], row["path"]


def test_external_mandatory_edit_invalidates_cached_bundle(fixture):
    principal = fixture.create()
    fixture.resolver.resolve_agent_config(principal)
    path, _ = foundation_source(fixture)
    path.write_text(path.read_text() + "\nChanged outside the service.\n")
    with pytest.raises(DomainError) as error:
        fixture.resolver.resolve_agent_config(principal)
    assert error.value.code == "document_conflict"


def test_mandatory_context_between_80k_and_120k_is_preserved(fixture):
    principal = fixture.create()
    path, relative = foundation_source(fixture)
    text = "# Foundation\n" + "f" * 81000
    path.write_text(text)
    with fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE documents SET content_hash=?,revision=revision+1 WHERE business_id IS NULL AND path=?", (content_hash(text.encode()), relative))
    data = fixture.resolver.resolve_agent_config(principal).to_dict()
    context = data["mandatory_context"]
    assert 80000 < context["character_count"] <= 120000
    assert "f" * 81000 in context["rendered"]


def test_missing_and_oversized_context_are_not_truncated(fixture):
    principal = fixture.create()
    path, relative = foundation_source(fixture)
    text = "# Foundation\n" + "f" * MAX_CONTEXT_CHARACTERS
    path.write_text(text)
    with fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE documents SET content_hash=?,revision=revision+1 WHERE business_id IS NULL AND path=?", (content_hash(text.encode()), relative))
    with pytest.raises(DomainError) as error:
        fixture.resolver.resolve_agent_config(principal)
    assert error.value.code == "context_budget_exceeded"
    path.unlink()
    with pytest.raises(DomainError) as error:
        fixture.resolver.resolve_agent_config(principal)
    assert error.value.code == "missing_context"


def test_settings_use_scoped_reconnecting_mcp_and_no_personal_identity(fixture, monkeypatch):
    principal = fixture.create()
    ready = fixture.prepare(principal)
    workspace = Path(ready["workspace"])
    connection = json.loads((workspace / ".mcp.json").read_text())["mcpServers"]["bazarlyk"]
    assert connection["type"] == "stdio" and connection["command"] == "/bin/sh"
    assert connection["args"] == [str(workspace / ".bazarlyk/mcp-bridge")]
    assert connection["timeout"] == 240000
    assert "bazarlyk.bootstrap.mcp_reconnect" in (workspace / ".bazarlyk/mcp-bridge").read_text()
    assert "headersHelper" not in connection and "url" not in connection
    settings = json.loads((workspace / ".claude/settings.local.json").read_text())
    assert set(settings["hooks"]) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                                     "PostToolUseFailure", "PermissionRequest", "Stop", "SessionEnd"}
    assert "enabledPlugins" not in json.loads((workspace / ".claude/settings.json").read_text())
    monkeypatch.setenv("BIZ_ACTOR", "human:owner")
    registration = load_registration(workspace / ".bazarlyk/registration.json")
    headers = auth_headers(registration)
    assert headers["Authorization"].startswith("Bearer ")
    assert "Bearer " not in (workspace / ".mcp.json").read_text()
    assert (workspace / ".bazarlyk/credential").stat().st_mode & 0o077 == 0
    assert ready["personal_sources"] == "not_inspected"
    assert not compatibility_report("2.1.0")["supported"]


@pytest.mark.parametrize("event", ["startup", "resume", "clear", "compact", "fork"])
def test_lifecycle_always_emits_full_context_and_receipt(fixture, event, monkeypatch):
    monkeypatch.setenv("QORQUT_TEST_VISIBLE_TOKEN", "never-emit-this-environment-value")
    principal = fixture.create()
    ready = fixture.prepare(principal)
    class Client:
        def __init__(self, registration):
            pass
        def post(self, route, values):
            return fixture.resolver.record_delivery(principal, **values) if route.endswith("receipt") else fixture.resolver.bootstrap(principal, **values)
    output = io.StringIO()
    path = Path(ready["workspace"]) / ".bazarlyk/registration.json"
    assert run({"hook_event_name": "SessionStart", "source": event, "session_id": "session-1"}, path,
               stream=output, diagnostics=io.StringIO(), client_factory=Client) == 0
    emitted = json.loads(output.getvalue())["hookSpecificOutput"]
    assert emitted["reloadSkills"] is True
    assert "Universal Foundation" in emitted["additionalContext"]
    assert "QORQUT_TEST_VISIBLE_TOKEN" in emitted["additionalContext"]
    assert "never-emit-this-environment-value" not in emitted["additionalContext"]
    assert len(emitted["additionalContext"]) <= MAX_CONTEXT_CHARACTERS
    assert fixture.resolver.resolve_agent_config(principal, session_registration={"registration_id": ready["registration_id"], "session_id": "session-1"}).to_dict()["delivery"]["lifecycle_event"] == event
    prompt_output = io.StringIO()
    run({"hook_event_name": "UserPromptSubmit", "session_id": "session-1"}, path, stream=prompt_output, diagnostics=io.StringIO(), client_factory=Client)
    assert json.loads(prompt_output.getvalue()) == {}


def test_bootstrap_errors_block_prompt_but_session_start_reports(fixture):
    missing = fixture.root / "missing/.bazarlyk/registration.json"
    for event in ("SessionStart", "UserPromptSubmit"):
        output = io.StringIO()
        run({"hook_event_name": event, "session_id": "test"}, missing, stream=output, diagnostics=io.StringIO())
        result = json.loads(output.getvalue())
        assert (result.get("decision") == "block") == (event == "UserPromptSubmit")
        assert "missing_registration" in output.getvalue()


def test_inactive_and_cross_business_principals_are_rejected(fixture):
    principal = fixture.create()
    with pytest.raises(DomainError) as error:
        fixture.resolver.resolve_agent_config(principal, business_id=new_id())
    assert error.value.code == "forbidden"
    with fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (principal.id,))
    with pytest.raises(DomainError) as error:
        fixture.resolver.resolve_agent_config(principal)
    assert error.value.code == "inactive_agent"


def test_permission_replacement_and_derived_effects(fixture):
    principal = fixture.create()
    profile, _ = fixture.resolver.profile(principal)
    with fixture.db.read() as uow:
        plan = fixture.ops.prepare("permissions_set", fixture.ctx,
            {"agent_id": principal.id, "overrides": {"agents_create": "allow"}},
            InvocationOptions(expected_revision=profile.revision, idempotency_key=new_id()), uow)
    assert fixture.resolver.profile(principal)[0].data["tool_policy"]["overrides"] == {}
    with fixture.db.transaction() as uow:
        result = fixture.ops.apply(fixture.ctx, plan, uow)
    assert result["overrides"] == {"agents_create": "allow"}
    assert result["profile_revision"] == 2
    generic = fixture.update(principal, {"tool_policy": {"overrides": {}}})
    assert "permissions_set" in {change.operation_name for change in generic.changes}
    assert fixture.resolver.profile(principal)[0].data["tool_policy"]["overrides"] == {}


def test_coordinator_creation_includes_initial_role_but_not_custom_permissions(fixture):
    coordinator = fixture.create("coordinator", "ordubashy", tool_policy={"overrides": {"agents_create": "allow"}})
    app, _ = fixture.application()
    ctx = fixture.resolver.invocation_context(coordinator, business_id=fixture.business_id)
    args = {"role": fixture.refs["business-agent"]}
    guarded = app.invoke(ctx, 'agents_create', {**args, 'tool_policy': {'overrides': {'tasks_create': 'signoff'}}},
                         {'idempotency_key': new_id()})
    assert guarded.status == 'signoff_required', guarded.to_dict()
    created = app.invoke(ctx, 'agents_create', args, {'idempotency_key': new_id()})
    assert created.status == 'ok', created.to_dict()


def test_retirement_refuses_outstanding_tasks_and_revokes_identity(fixture):
    agent = fixture.create()
    principal = fixture.create("marketing", "global-marketing")
    fixture.prepare(principal)
    with fixture.db.transaction() as uow:
        uow.repository("tasks", business_id=fixture.business_id).insert({"id": "T1", "title": "Outstanding", "status": "open", "created_by": fixture.owner.id})
        uow.repository("relationships", business_id=fixture.business_id).insert({"id": new_id(), "from_type": "task", "from_id": "T1", "to_type": "agent", "to_id": principal.id, "role": "assigned_to"})
    options = InvocationOptions(expected_revision=1, idempotency_key=new_id())
    owner = replace(fixture.ctx, business_id=None, review_business_id=None)
    with fixture.db.read() as uow, pytest.raises(DomainError) as error:
        fixture.ops.prepare("agents_deactivate", owner, {"agent_id": principal.id}, options, uow)
    assert error.value.code == "agent_has_open_tasks"
    with fixture.db.transaction() as uow:
        uow.repository("tasks", business_id=fixture.business_id).update("T1", {"status": "done"}, expected_revision=1)
    with fixture.db.read() as uow:
        plan = fixture.ops.prepare("agents_deactivate", owner, {"agent_id": principal.id}, options, uow)
    with fixture.db.transaction() as uow:
        fixture.ops.apply(owner, plan, uow)
    with fixture.db.read() as uow:
        assert all(row["revoked_at"] for row in uow.repository("credentials").list(filters={"principal_id": principal.id}))
    with pytest.raises(DomainError):
        fixture.resolver.resolve_agent_config(principal)
    # The business keeps its one execution agent while it is not archived.
    with fixture.db.read() as uow, pytest.raises(DomainError) as error:
        fixture.ops.prepare("agents_deactivate", fixture.ctx, {"agent_id": agent.id}, options, uow)
    assert error.value.code == "roster_conflict"


def test_retirement_uses_configured_state_kind_not_conventional_name(fixture):
    from copy import deepcopy
    from bazarlyk.domains.tasks.models import DEFAULT_CONFIG

    principal = fixture.create()
    configuration = deepcopy(DEFAULT_CONFIG)
    configuration["states"] = [{"name": "done", "kind": "open", "default": True}, {"name": "archived", "kind": "closed"}]
    with fixture.db.transaction() as uow:
        uow.repository("business_configs", business_id=fixture.business_id).insert({"domain": "tasks", "config_json": configuration})
        uow.repository("tasks", business_id=fixture.business_id).insert({"id": "T1", "title": "Still open", "status": "done", "created_by": fixture.owner.id})
        uow.repository("relationships", business_id=fixture.business_id).insert({"id": new_id(), "from_type": "task", "from_id": "T1", "to_type": "agent", "to_id": principal.id, "role": "assigned_to"})
    profile, _ = fixture.resolver.profile(principal)
    with fixture.db.read() as uow, pytest.raises(DomainError) as error:
        fixture.ops._retirement_ready(principal.id, profile, uow)
    assert error.value.code == "agent_has_open_tasks"
    with fixture.db.transaction() as uow:
        uow.repository("tasks", business_id=fixture.business_id).update("T1", {"status": "archived"}, expected_revision=1)
    with fixture.db.read() as uow:
        fixture.ops._retirement_ready(principal.id, profile, uow)


def test_published_context_text_is_the_orientation_and_authored_pillars_are_not_injected_or_required(fixture):
    """The shared published graph text is the one current Context brief; the authored Product/ICP/Brand documents are
    ordinary scoped sources: not rendered beside it, not a fallback and not required for bootstrap."""
    principal = fixture.create()
    bare = fixture.resolver.resolve_agent_config(principal).to_dict()["mandatory_context"]["rendered"]
    assert "Core Product" not in bare and "Context Home" not in bare and "not provided in this configuration" in bare
    fixture.resolver.context_home = lambda business_id: ("Shared Context (published graph)\nRevision abc: 1 nodes.\n"
                                                         "- node:widgt \u2014 Widgets for testers")
    rendered = fixture.resolver.resolve_agent_config(principal).to_dict()["mandatory_context"]["rendered"]
    assert "Context Home" in rendered and "node:widgt" in rendered and "context_find" in rendered
    assert "Core Product" not in rendered and "Core Icp" not in rendered and "Core Brand" not in rendered
    for retired in ("living brief", "About The Owner", "Latest:", "context-base:"):
        assert retired not in rendered, retired
    path = fixture.hq / "context/product.md"  # an external edit, then removal, of a pillar file no longer touches bootstrap
    original = path.read_text()
    path.write_text(original.replace("Unknown", "Changed"))
    assert "Context Home" in fixture.resolver.resolve_agent_config(principal).to_dict()["mandatory_context"]["rendered"]
    path.unlink()
    assert "Context Home" in fixture.resolver.resolve_agent_config(principal).to_dict()["mandatory_context"]["rendered"]
