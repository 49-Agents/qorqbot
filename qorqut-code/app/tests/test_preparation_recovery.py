from contextlib import contextmanager
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from bazarlyk.application.registry import OperationDescriptor
from bazarlyk.background.integration import attach_background
from qorqut_operations.background.programs import ProgramRegistry
from bazarlyk.bootstrap.preparation import prepare_agent
from qorqut_operations.contracts import DomainError, canonical_hash
from bazarlyk.service.runtime import build_runtime
from bazarlyk.storage import safe_io
from test_service_transports import ServiceFixture
from test_agent_configuration import REPO, PATHS


VERSIONS = {"claude-code": "2.1.263 (Claude Code)", "codex": "codex-cli 0.153.4"}


def registration(service):
    with service.fixture.db.read() as uow:
        return uow.repository("machine_registrations").get(service.registrations[service.agent.id])


def bootstrap(service, runtime="claude-code"):
    return service.fixture.resolver.bootstrap(service.agent,
        registration_id=service.registrations[service.agent.id], session_id="existing-session",
        event="compact", runtime=runtime)


def prepare_both(service):
    return prepare_agent(service.fixture.resolver, service.agent,
        endpoint="http://127.0.0.1:9988", versions=VERSIONS)


def deny_task_creation(service):
    service.fixture.update(service.agent, {"tool_policy": {"overrides": {"tasks_create": "deny"}}})


@pytest.mark.parametrize("legacy", [False, True])
def test_unrelated_registry_changes_do_not_block_existing_sessions(tmp_path, legacy):
    service = ServiceFixture(tmp_path)
    ready = prepare_both(service)
    if legacy:
        before = registration(service)
        config = service.fixture.resolver.resolve_agent_config(service.agent).to_dict()
        projection = before["projection_json"]
        projection.pop("tool_configuration_version")
        projection["tool_configuration"] = canonical_hash([
            {key: row[key] for key in ("canonical_operation", "exposed_aliases", "effective_mode", "available")}
            for row in config["tools"]])
        with service.fixture.db.transaction() as uow:
            uow.repository("machine_registrations").update(before["id"], {"projection_json": projection},
                expected_revision=before["revision"])
    before = registration(service)
    settings = Path(ready["workspace"]) / ".claude/settings.json"
    original = settings.read_bytes()
    for name, exposure in (("fixture_dashboard_summary", {"dashboard"}), ("fixture_denied_tool", {"mcp"})):
        service.fixture.registry.register(OperationDescriptor(name, {"type": "object"},
            effect_kind="read", supports_signoff=False, exposure=exposure, read=lambda *args: {}))
    for runtime in VERSIONS:
        assert bootstrap(service, runtime)["ready"]
    assert registration(service) == before
    assert settings.read_bytes() == original


def test_bootstrap_repairs_permission_changes_without_changing_other_files_or_credentials(tmp_path):
    service = ServiceFixture(tmp_path)
    ready = prepare_both(service)
    workspace = Path(ready["workspace"])
    before = registration(service)
    saved = {name: (workspace / name).read_bytes() for name in before["projection_json"]["files"]}
    token = (workspace / ".bazarlyk/credential").read_bytes()
    deny_task_creation(service)
    status = service.fixture.resolver.resolve_agent_config(service.agent).to_dict()["preparation"]["runtimes"]
    assert status["codex"]["ready"]
    assert status["claude-code"]["projection_drift"] == ["tool_configuration_changed"]
    # Read-only resolution and Codex bootstrap must not rewrite Claude files.
    assert bootstrap(service, "codex")["ready"]
    assert registration(service) == before
    with TestClient(service.web) as client:
        response = client.post("/api/agent/bootstrap", json={"registration_id": ready["registration_id"],
            "session_id": "existing-session", "event": "compact", "runtime": "claude-code"},
            headers={"Authorization": "Bearer " + service.tokens[service.agent.id]})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["ready"]
    native = json.loads((workspace / ".claude/settings.json").read_text())["permissions"]
    assert "mcp__bazarlyk__tasks_create" not in native["allow"]
    assert "mcp__bazarlyk__tasks_create" in native["deny"]
    assert (workspace / ".bazarlyk/credential").read_bytes() == token
    assert registration(service)["credential_id"] == before["credential_id"]
    for name, content in saved.items():
        if name != ".claude/settings.json":
            assert (workspace / name).read_bytes() == content
    service.fixture.update(service.agent, {"tool_policy": {"overrides": {}}})
    assert bootstrap(service)["ready"]
    native = json.loads((workspace / ".claude/settings.json").read_text())["permissions"]
    assert "mcp__bazarlyk__tasks_create" in native["allow"]
    assert "mcp__bazarlyk__tasks_create" not in native["deny"]
    current = registration(service)
    assert bootstrap(service)["ready"]
    assert registration(service) == current


def test_real_file_and_credential_drift_stays_blocked_and_explained(tmp_path):
    service = ServiceFixture(tmp_path)
    ready = prepare_both(service)
    workspace = Path(ready["workspace"])
    settings = workspace / ".claude/settings.json"
    original = settings.read_bytes()
    settings.write_bytes(original + b"\n")
    deny_task_creation(service)
    before = registration(service)
    blocked = bootstrap(service)
    assert not blocked["ready"]
    assert ".claude/settings.json" in blocked["error"]["message"]
    assert "reprepare this agent" in blocked["error"]["message"]
    assert registration(service) == before
    assert settings.read_bytes() == original + b"\n"
    assert bootstrap(service, "codex")["ready"]
    settings.write_bytes(original)
    (workspace / ".bazarlyk/credential").unlink()
    for runtime in VERSIONS:
        blocked = bootstrap(service, runtime)
        assert not blocked["ready"]
        assert "credential" in blocked["error"]["message"]
    assert registration(service) == before


@pytest.mark.parametrize("failure", ["transaction", "file_sync"])
def test_failed_permission_refresh_restores_original_files(tmp_path, monkeypatch, failure):
    service = ServiceFixture(tmp_path)
    ready = prepare_both(service)
    deny_task_creation(service)
    before = registration(service)
    settings = Path(ready["workspace"]) / ".claude/settings.json"
    original = settings.read_bytes()
    transaction = service.fixture.db.transaction

    @contextmanager
    def failed_commit(**kwargs):
        with transaction(**kwargs) as uow:
            yield uow
            raise DomainError("storage_error", "Injected failure before commit")

    if failure == "transaction":
        monkeypatch.setattr(service.fixture.db, "transaction", failed_commit)
    else:
        atomic_write = safe_io.atomic_write

        def failed_sync(path, content, **kwargs):
            atomic_write(path, content, **kwargs)
            if content != original:
                raise OSError("Injected failure after replacement")

        monkeypatch.setattr(safe_io, "atomic_write", failed_sync)
    blocked = bootstrap(service)
    assert not blocked["ready"]
    if failure == "transaction":
        assert "Injected failure before commit" in blocked["error"]["message"]
    assert settings.read_bytes() == original
    assert registration(service) == before


def change_role_skill(monkeypatch):
    from bazarlyk.agents import role_workflow
    body = role_workflow.skill_files()["SKILL.md"] + b"\nChanged by a core release.\n"
    monkeypatch.setattr(role_workflow, "skill_files", lambda: {"SKILL.md": body})
    return body


def test_bootstrap_refreshes_changed_skill_instructions_without_other_changes(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path)
    ready = prepare_both(service)
    workspace = Path(ready["workspace"])
    before = registration(service)
    saved = {name: (workspace / name).read_bytes() for name in before["projection_json"]["files"]}
    token = (workspace / ".bazarlyk/credential").read_bytes()
    body = change_role_skill(monkeypatch)
    status = service.fixture.resolver.resolve_agent_config(service.agent).to_dict()["preparation"]["runtimes"]
    assert all(row["projection_drift"] == ["selected_skills_changed"] for row in status.values())
    assert bootstrap(service)["ready"]
    after = registration(service)
    for directory in (".claude/skills/role", ".agents/skills/role"):
        assert (workspace / directory / "SKILL.md").read_bytes() == body
    for name, content in saved.items():
        if not name.endswith("skills/role/SKILL.md"):
            assert (workspace / name).read_bytes() == content
    assert (workspace / ".bazarlyk/credential").read_bytes() == token
    assert after["credential_id"] == before["credential_id"]
    assert after["settings_digest"] == before["settings_digest"]
    assert after["skills_digest"] != before["skills_digest"]
    # Both CLIs are current after one refresh; later bootstraps write nothing.
    assert bootstrap(service, "codex")["ready"]
    assert bootstrap(service)["ready"]
    assert registration(service) == after


@pytest.mark.parametrize("change", ["edited_file", "skill_set"])
def test_skill_refresh_stays_blocked_for_edited_files_or_a_changed_skill_set(tmp_path, monkeypatch, change):
    service = ServiceFixture(tmp_path)
    ready = prepare_both(service)
    workspace = Path(ready["workspace"])
    edited = workspace / ".agents/skills/role/SKILL.md"
    if change == "edited_file":
        edited.write_bytes(edited.read_bytes() + b"\nOwner note.\n")
    else:
        before = registration(service)
        projection = before["projection_json"]
        projection["selected_skills"] = {**projection["selected_skills"], "bazarlyk-retired": "sha256:" + "0" * 64}
        with service.fixture.db.transaction() as uow:
            uow.repository("machine_registrations").update(before["id"], {"projection_json": projection},
                expected_revision=before["revision"])
    saved = {path: path.read_bytes() for path in (edited, workspace / ".claude/skills/role/SKILL.md")}
    before = registration(service)
    change_role_skill(monkeypatch)
    blocked = bootstrap(service)
    assert not blocked["ready"]
    assert "The selected skill instructions changed" in blocked["error"]["message"]
    assert registration(service) == before
    assert {path: path.read_bytes() for path in saved} == saved


def test_failed_skill_refresh_restores_original_files(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path)
    ready = prepare_both(service)
    workspace = Path(ready["workspace"])
    saved = {path: path.read_bytes() for path in (workspace / ".claude/skills/role/SKILL.md",
                                                  workspace / ".agents/skills/role/SKILL.md")}
    before = registration(service)
    change_role_skill(monkeypatch)
    transaction = service.fixture.db.transaction

    @contextmanager
    def failed_commit(**kwargs):
        with transaction(**kwargs) as uow:
            yield uow
            raise DomainError("storage_error", "Injected failure before commit")

    monkeypatch.setattr(service.fixture.db, "transaction", failed_commit)
    blocked = bootstrap(service)
    assert not blocked["ready"]
    assert "Injected failure before commit" in blocked["error"]["message"]
    assert {path: path.read_bytes() for path in saved} == saved
    assert registration(service) == before


def test_maintenance_and_service_assembly_use_identical_registry(tmp_path):
    service = ServiceFixture(tmp_path)
    programs = ProgramRegistry()
    runtime = build_runtime(service.fixture.root, REPO, paths=PATHS, database=service.fixture.db)
    assert runtime.background is None
    before = runtime.resolver.resolve_agent_config(service.agent).to_dict()["tools"]
    prepared = prepare_agent(runtime.resolver, service.agent, endpoint="http://127.0.0.1:9988", versions=VERSIONS)
    assert all(row["ready"] for row in prepared["runtimes"].values())
    attach_background(runtime, programs)
    after = runtime.resolver.resolve_agent_config(service.agent).to_dict()
    assert before == after["tools"]
    assert all(row["ready"] for row in after["preparation"]["runtimes"].values())
