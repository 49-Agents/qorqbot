"""Focused native projection checks using real host state and explicit peer fakes."""
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from bazarlyk.bootstrap import preparation
from bazarlyk.catalog.packages import content_hash
from qorqut_operations.contracts import DomainError, Principal
from agentic_projection_support import (ATTRIBUTION, fake_c_interfaces, opened_view, skill_body)
def owner_edit(layer, path, content):
    """Fixture owner performs host maintenance on protected areas and scans it."""
    target = layer.root / path
    target.write_text(content)
    layer.changes.scan()


def edit(layer, path, content):
    if path.startswith(("roles/", "operations/", "overseer/")):
        return owner_edit(layer, path, content)
    from bazarlyk.agentic.support import digest
    layer.authoring.apply(ATTRIBUTION, [{"op": "edit", "path": path,
        "expected_sha256": digest((layer.root / path).read_bytes()), "content": content}])


@pytest.fixture
def host(tmp_path, monkeypatch):
    from host_foundation_support import build_instance, create_worker, publish_role, register_sources
    monkeypatch.setattr("bazarlyk.installation.helper_launcher", lambda: None)
    monkeypatch.setattr(preparation, "checkpoint_protocols", lambda *args: {"claude-code": 0, "codex": 0})
    monkeypatch.setattr("bazarlyk.bootstrap.diagnostics.prepared_helpers", lambda *args: {"ready": True, "reason": "synthetic"})
    instance, _ = build_instance(tmp_path)
    register_sources(instance, instance.business, "first")
    pin = publish_role(instance, "global-worker")
    package = instance.runtime.catalog.get("role", pin)
    view, inventory = opened_view(tmp_path / "managed", [package], preferences=True)
    instance.runtime.resolver.agentic_view = view
    fake_c_interfaces(monkeypatch)
    principal = Principal(create_worker(instance, "projection-worker"), "agent")
    yield SimpleNamespace(instance=instance, resolver=instance.runtime.resolver, principal=principal,
                          view=view, inventory=inventory, package=package, monkeypatch=monkeypatch)
    instance.database.close()


def ready(host):
    return preparation.prepare_agent(host.resolver, host.principal,
        endpoint="http://127.0.0.1:9988", interpreter=sys.executable,
        interpreter_validator=lambda executable: True,
        versions={"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.153.4"})


def test_edits_change_context_metadata_and_native_files_without_grants(host):
    before = host.resolver.resolve_agent_config(host.principal).to_dict()
    first = ready(host)
    workspace = Path(first["workspace"])
    registration_before = (workspace / ".bazarlyk/registration.json").read_bytes()
    credential_before = (workspace / ".bazarlyk/credential").read_bytes()
    assert (workspace / ".bazarlyk/agentic").stat().st_mode & 0o100
    edit(host.view.layer, "operations/imported-host-builtins/native/app/bazarlyk/agents/skills/role/SKILL.md",
         skill_body("role", "Edited managed role procedure.").decode())
    edit(host.view.layer, "roles/imported-roles/native/" + host.package.manifest["brief"], "Edited current role brief.")
    after = host.resolver.resolve_agent_config(host.principal).to_dict()
    assert after["effective_config_id"] != before["effective_config_id"]
    assert "Edited current role brief." in after["mandatory_context"]["rendered"]
    assert shlex.quote(str(workspace / ".bazarlyk/agentic")) in after["mandatory_context"]["rendered"]
    assert after["role"] == before["role"] and after["plugins"] == before["plugins"] and after["tools"] == before["tools"]
    assert after["native_guidance_digest"] == before["native_guidance_digest"]
    assert "native_guidance_changed" not in after["preparation"]["projection_drift"]
    assert not after["preparation"]["ready"]
    from bazarlyk.bootstrap import skill_refresh
    assert skill_refresh.refresh(host.resolver, host.principal,
        host.resolver.resolve_agent_config(host.principal), first["registration_id"], "codex")
    assert b"Edited managed role procedure." in (workspace / ".agents/skills/role/SKILL.md").read_bytes()
    assert b"Edited managed role procedure." in (workspace / ".claude/skills/role/SKILL.md").read_bytes()
    assert host.resolver.resolve_agent_config(host.principal).to_dict()["preparation"]["ready"]
    assert (workspace / ".bazarlyk/registration.json").read_bytes() == registration_before
    assert (workspace / ".bazarlyk/credential").read_bytes() == credential_before


def test_toolkit_has_one_composer_owned_section_and_source_after_edits(host):
    before = host.resolver.resolve_agent_config(host.principal).to_dict()
    command = shlex.quote(str(Path(before["workspace"]) / ".bazarlyk/agentic"))

    def check(config, instruction):
        mandatory = config["mandatory_context"]
        assert mandatory["rendered"].casefold().count("## agentic toolkit") == 1
        assert mandatory["rendered"].count(instruction) == 1
        assert mandatory["rendered"].count(command) == 1
        sources = [entry for entry in mandatory["entries"]
                   if "toolkit" in entry["source_id"] or "toolkit" in entry["source_path"]]
        assert sources == [{"document_id": "core:agentic-toolkit", "source_id": "core:agentic-toolkit",
                            "source_kind": "core", "source_path": "qorqut-core:agentic-toolkit",
                            "section_selector": None, "revision": 1, "hash": content_hash(instruction.encode()),
                            "required": True, "character_count": len(instruction),
                            "inclusion_reason": "Current native authoring, catalog and progress guidance"}]

    check(before, "Run " + command + ". Read the catalog before work.\n")
    edited = "Changed toolkit instruction: {{TOOLKIT_COMMAND}}.\n"
    edit(host.view.layer, "operations/imported-host-builtins/native/app/bazarlyk/agentic/assets/toolkit.md", edited)
    after = host.resolver.resolve_agent_config(host.principal).to_dict()
    check(after, edited.replace("{{TOOLKIT_COMMAND}}", command))
    assert "Read the catalog before work." not in after["mandatory_context"]["rendered"]
    assert after["effective_config_id"] != before["effective_config_id"]


def test_helper_uses_exact_registration_and_installed_launcher(host):
    config = host.resolver.resolve_agent_config(host.principal)
    host.monkeypatch.setattr("bazarlyk.installation.helper_launcher", lambda: "/copied install/bin/agent-helper")
    projection = preparation.build_projection(host.resolver, config, registration_id="fixture-registration",
        endpoint="http://127.0.0.1:9988", interpreter=sys.executable, interpreter_validator=lambda _: True)
    expected = shlex.join(["/copied install/bin/agent-helper", "bazarlyk.agentic.client", "--registration",
                          str(Path(config.data["workspace"]) / ".bazarlyk/registration.json")])
    assert projection.files[".bazarlyk/agentic"] == ("#!/bin/sh\nexec " + expected + "\n").encode()


def test_projection_rejects_configuration_from_previous_catalog_revision(host):
    config = host.resolver.resolve_agent_config(host.principal)
    edit(host.view.layer, "roles/imported-roles/native/" + host.package.manifest["brief"], "Later.")
    with pytest.raises(DomainError) as error:
        preparation.skill_projection(host.resolver, config.to_dict())
    assert error.value.code == "agentic_revision_conflict"


@pytest.mark.parametrize("conflict", ["manual-edit", "names"])
def test_safe_refresh_refuses_foreign_edits_and_changed_native_names(host, conflict):
    first = ready(host)
    config = host.resolver.resolve_agent_config(host.principal)
    workspace = Path(config.data["workspace"])
    target = workspace / ".agents/skills/role/SKILL.md"
    if conflict == "manual-edit":
        target.write_bytes(target.read_bytes() + b"Owner edit.\n")
    else:
        with host.instance.database.transaction() as uow:
            repo = uow.repository("machine_registrations")
            row = repo.get(first["registration_id"])
            projection = row["projection_json"]
            projection["selected_skills"]["bazarlyk-old-name"] = "sha256:" + "0" * 64
            repo.update(row["id"], {"projection_json": projection}, expected_revision=row["revision"])
    edit(host.view.layer, "operations/imported-host-builtins/native/app/bazarlyk/agents/skills/role/SKILL.md",
         skill_body("role", "Current revised body.").decode())
    saved = target.read_bytes()
    config = host.resolver.resolve_agent_config(host.principal)
    from bazarlyk.bootstrap import skill_refresh
    assert not skill_refresh.refresh(host.resolver, host.principal, config, first["registration_id"], "codex")
    assert target.read_bytes() == saved
    assert config.data["preparation"]["status"] == "reprepare_required"


def test_missing_view_has_no_pinned_context_fallback(host):
    host.resolver.agentic_view = None
    with pytest.raises(DomainError) as error:
        host.resolver.resolve_agent_config(host.principal)
    assert error.value.code == "agentic_unavailable"


def test_native_connection_callbacks_receive_the_managed_view(host):
    calls = []
    def orientation(prepared, ordubashy, *, agentic_view):
        calls.append(("orientation", prepared, agentic_view))
        return "Managed extension orientation.\n"
    def guidance(*, agentic_view):
        calls.append(("guidance", True, agentic_view))
        return "Managed extension guidance.\n"
    connection = SimpleNamespace(server="synthetic-extension", retires=(),
        status=lambda *args: {"prepared": True}, entry=lambda *args: {"command": "/synthetic/not-executed"},
        files=lambda *args: {}, orientation=orientation, guidance=guidance)
    host.resolver.native_connections = {"synthetic": connection}
    host.monkeypatch.setattr(preparation, "external_connections", lambda *args: ["synthetic"])
    config = host.resolver.resolve_agent_config(host.principal)
    projection = preparation.build_projection(host.resolver, config, registration_id="synthetic",
        endpoint="http://127.0.0.1:9988", interpreter=sys.executable, interpreter_validator=lambda _: True)
    assert {row[:2] for row in calls} == {("orientation", True), ("orientation", False), ("guidance", True)}
    assert all(row[2] is host.view for row in calls)
    assert projection.metadata["native_guidance_digest"] == config.data["native_guidance_digest"]
    assert b"Managed extension guidance." in projection.files["AGENTS.md"]


def test_mail_preparation_validates_canonical_guide_and_never_seeds(host):
    from bazarlyk.bootstrap import email_guidance
    import yaml
    target = host.view.layer.root / "plugins/imported-email-management/guidance/email"
    changes = [{"op": "create", "path": "plugins/imported-email-management/agentic.yaml", "content": yaml.safe_dump({
        "schema_version": 1, "id": "imported-email-management", "name": "Mail", "description": "Synthetic mail",
        "entries": [{"id": "guide", "kind": "skill", "path": "guidance/email/SKILL.md",
                     "description": "Mail instructions", "when_to_use": "Mail"}]})}]
    for name in email_guidance.TEMPLATE_FILES:
        changes.append({"op": "create", "path": "plugins/imported-email-management/guidance/email/" + name,
                        "content": skill_body("email").decode() if name == "SKILL.md" else "Synthetic mail guide."})
    host.view.layer.authoring.apply(ATTRIBUTION, changes)
    def home(paths, *, agentic_view):
        resource = agentic_view.layer.catalog.read_resource("imported-email-management", "guidance/email/SKILL.md")
        return (agentic_view.layer.root / resource["root_path"]).parent
    host.monkeypatch.setattr(email_guidance, "home", home)
    host.monkeypatch.setattr(email_guidance, "seed", lambda *args: pytest.fail("preparation must never seed mail"), raising=False)
    data = {"plugins": [{"id": "email-management"}], "agentic_selection": host.view.selection({}),
            "agentic_catalog_revision": host.view.catalog_revision}
    result = preparation.seed_instance_guidance(host.resolver, data)
    assert result["email-management"] == {"path": str(target), "source": "agentic", "seeded": False}
    assert not (target / ".origin.json").exists()


def test_refresh_rolls_back_if_catalog_changes_during_native_write(host):
    first = ready(host)
    workspace = Path(first["workspace"])
    target = workspace / ".agents/skills/role/SKILL.md"
    before = target.read_bytes()
    edit(host.view.layer, "operations/imported-host-builtins/native/app/bazarlyk/agents/skills/role/SKILL.md",
         skill_body("role", "Revised before refresh.").decode())
    config = host.resolver.resolve_agent_config(host.principal)
    from bazarlyk.bootstrap import skill_refresh
    writer = skill_refresh.safe_io.atomic_write
    changed = False
    def conflict(path, *args, **kwargs):
        nonlocal changed
        result = writer(path, *args, **kwargs)
        if not changed and Path(path) == target:
            changed = True
            edit(host.view.layer, "operations/imported-host-builtins/native/app/bazarlyk/agentic/assets/toolkit.md",
                 "Changed during projection {{TOOLKIT_COMMAND}}.")
        return result
    host.monkeypatch.setattr(skill_refresh.safe_io, "atomic_write", conflict)
    with pytest.raises(DomainError) as error:
        skill_refresh.refresh(host.resolver, host.principal, config, first["registration_id"], "codex")
    assert error.value.code == "agentic_revision_conflict"
    assert changed and target.read_bytes() == before


def test_local_catalog_observation_uses_host_binding(host):
    # Real engine plus synthetic host binding.
    from bazarlyk.bootstrap.catalog_revision import observe
    from qorqut_agentic import AgenticLayer
    paths = host.instance.paths
    layer = AgenticLayer.open(paths.agentic_root, paths.agentic_runtime)
    assert observe(paths.instance_root, 2.0) == layer.catalog.list(limit=1)['catalog_revision']


def test_local_catalog_observation_is_bounded_when_authoring_lock_is_busy(host):
    import fcntl
    import os
    import time
    from bazarlyk.bootstrap.catalog_revision import observe
    from qorqut_agentic import AgenticLayer
    paths = host.instance.paths
    layer = AgenticLayer.open(paths.agentic_root, paths.agentic_runtime)
    fd = os.open(layer.catalog.ctx.store.lock_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(DomainError) as error:
            observe(paths.instance_root, .3)
        assert error.value.code == 'bootstrap_pending'
        assert time.monotonic() - started < 1.0
    finally:
        os.close(fd)


def test_synthetic_preferences_refresh_on_catalog_revision_without_pin_change(host):
    before = host.resolver.resolve_agent_config(host.principal).to_dict()
    preferences = '---\nname: preferences\ndescription: Fixture\n---\nEdited preferences in full.\n'
    edit(host.view.layer, 'user-preferences/SKILL.md', preferences)
    edit(host.view.layer, 'roles/imported-roles/native/' + host.package.manifest['brief'],
         'Edited guidance for the current catalogue revision.')
    after = host.resolver.resolve_agent_config(host.principal).to_dict()
    assert after['effective_config_id'] != before['effective_config_id']
    assert preferences in after['mandatory_context']['rendered']
    assert 'Edited guidance for the current catalogue revision.' in after['mandatory_context']['rendered']
    assert (after['role'], after['plugins'], after['tools']) == (before['role'], before['plugins'], before['tools'])
