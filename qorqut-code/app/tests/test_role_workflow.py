"""Core skill installation/lifecycle contracts; agent behavior is owner-tested."""
import json
from pathlib import Path

import pytest

from bazarlyk.agents import role_workflow
from bazarlyk.agents import cleanup_workflow
from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.catalog.packages import content_hash, files_digest
from qorqut_operations.contracts import DomainError, new_id
from test_agent_configuration import Fixture
from test_agent_inbox import inbox, story, subscribe, edit


VERSIONS = {"claude-code": "2.1.267 (Claude Code)", "codex": "codex-cli 0.154.0"}


def prepare(fixture, agent, **kwargs):
    return prepare_agent(fixture.resolver, agent, endpoint="http://127.0.0.1:9988",
                         versions=VERSIONS, **kwargs)


def test_core_role_workflow_reconciles_fresh_inbox_pages_without_widening_actions():
    instructions = ' '.join(role_workflow.INSTRUCTIONS.replace('`', '').split())
    from bazarlyk.inbox.defaults import INBOX_INSTRUCTIONS
    inbox = ' '.join(INBOX_INSTRUCTIONS.replace('`', '').split())
    for text in ('fresh inbox_list call', 'omitting after (or using 0)',
                 'Follow next_cursor with after only within that bounded pass',
                 'notification watermark', 'explicitly defer',
                 'acknowledgment does not complete underlying work',
                 'Prefer patch actions add/replace/remove', 'An empty patch confirms unchanged work'):
        assert text in instructions
    for text in ('For a fresh reconciliation, call inbox_list without after',
                 'notification watermark is not proof', 'owner_conversation_get',
                 'next_before', 'recording an explicit defer',
                 'does not complete the underlying task',
                 'exact IDs in the event_ids array', 'event_ids: [123]'):
        assert text in inbox
    assert 'exact `event_id`' not in inbox


def test_cleanup_skill_contract_and_both_client_materializations(tmp_path):
    skill = ' '.join(cleanup_workflow.skill_files()["SKILL.md"].decode().replace('`', '').split())
    for phrase in ("clean up its inbox", "does not clean files", "every authorized thread",
                   "older unresolved items behind newer handled messages", "approved Signoff does not prove",
                   "id=null is automatic-only", "complete writable payload", "partial coverage"):
        assert phrase in skill
    assert cleanup_workflow.skill_metadata()["owner"] == {"kind": "core", "id": "cleanup-workflow"}
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    before = fixture.resolver.resolve_agent_config(agent).to_dict()
    ready = prepare(fixture, agent)
    workspace = Path(ready["workspace"])
    metadata = next(item for item in before["skills"] if item["name"] == cleanup_workflow.SKILL_NAME)
    for directory in cleanup_workflow.SKILL_DIRECTORIES:
        actual = {"SKILL.md": (workspace / directory / "SKILL.md").read_bytes()}
        assert actual == cleanup_workflow.skill_files()
        assert files_digest(actual) == metadata["materialization_hash"]
    after = fixture.resolver.resolve_agent_config(agent).to_dict()
    for key in ("role", "plugins", "scope", "tool_policy", "tools", "profile_revision"):
        assert after[key] == before[key]
    claude_skill = workspace / cleanup_workflow.SKILL_DIRECTORIES[0] / "SKILL.md"
    claude_skill.write_text("drift")
    assert prepare(fixture, agent)["ready"]
    assert claude_skill.read_bytes() == cleanup_workflow.skill_files()["SKILL.md"]


def test_cleanup_skill_change_invalidates_and_refreshes_both_clients(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    agent = fixture.create(reddit=True)
    ready = prepare(fixture, agent)
    workspace = Path(ready["workspace"])
    before = fixture.resolver.resolve_agent_config(agent).to_dict()
    old_hash = cleanup_workflow.skill_metadata()["materialization_hash"]
    preserved = {}
    for skill in before["skills"]:
        if skill["name"] == cleanup_workflow.SKILL_NAME:
            continue
        for directory in (".claude/skills", ".agents/skills"):
            path = workspace / directory / skill["name"] / "SKILL.md"
            if path.is_file():
                preserved[path.relative_to(workspace).as_posix()] = path.read_bytes()
    assert any(skill["name"] != cleanup_workflow.SKILL_NAME and skill["owner"]["kind"] == "plugin"
               for skill in before["skills"])
    changed = {"SKILL.md": cleanup_workflow.skill_files()["SKILL.md"] + b"\nRefresh regression.\n"}
    monkeypatch.setattr(cleanup_workflow, "skill_files", lambda: changed)
    after = fixture.resolver.resolve_agent_config(agent).to_dict()
    new_hash = cleanup_workflow.skill_metadata()["materialization_hash"]
    assert new_hash != old_hash
    assert after["effective_config_id"] != before["effective_config_id"]
    assert not after["preparation"]["ready"]
    assert "selected_skills_changed" in after["preparation"]["projection_drift"]
    assert prepare(fixture, agent)["ready"]
    for directory in cleanup_workflow.SKILL_DIRECTORIES:
        path = workspace / directory / "SKILL.md"
        assert path.read_bytes() == changed["SKILL.md"]
    refreshed = fixture.resolver.resolve_agent_config(agent).to_dict()
    assert refreshed["preparation"]["ready"]
    assert refreshed["role"] == before["role"]
    for relative, body in preserved.items():
        assert (workspace / relative).read_bytes() == body


def test_cleanup_activity_guidance_respects_replace_schema_and_visibility():
    instructions = ' '.join(cleanup_workflow.skill_files()["SKILL.md"].decode().replace('`', '').split())
    for text in ("agents_activity_list(session_id=your_native_session_id)", "items_replacement_safe=true",
                 "use targeted patches rather than clearing unseen commitments", "work_revision", "enriched display DTOs", "may omit entries",
                 "business_id, task_id, summary, and blocked_reason",
                 "business_slug, business_name, blocked, and blockers"):
        assert text in instructions


def test_inbox_acknowledgement_guidance_matches_registered_array_schema(inbox):
    from bazarlyk.inbox.defaults import INBOX_INSTRUCTIONS

    schema = inbox.runtime.application.registry.get('inbox_acknowledge').input_schema
    assert schema['properties']['event_ids'] == {
        'type': 'array', 'minItems': 1, 'maxItems': 100, 'uniqueItems': True,
        'items': {'type': 'integer', 'minimum': 1}}
    assert 'exact IDs in the `event_ids` array' in INBOX_INSTRUCTIONS
    assert 'event_ids: [123]' in INBOX_INSTRUCTIONS


@pytest.mark.parametrize("role", ["business-agent", "ordubashy", "global-marketing"])
def test_role_skill_is_core_owned_and_preparation_keeps_authority(tmp_path, role):
    fixture = Fixture(tmp_path)
    agent = fixture.create() if role == "business-agent" else fixture.create(role, role)
    before = fixture.resolver.resolve_agent_config(agent).to_dict()
    result = prepare(fixture, agent)
    assert all(row["ready"] for row in result["runtimes"].values())
    workspace = Path(result["workspace"])
    metadata = next(skill for skill in before["skills"] if skill["name"] == "role")
    assert metadata["owner"] == {"kind": "core", "id": "role-workflow"}
    for directory in role_workflow.SKILL_DIRECTORIES:
        actual = {"SKILL.md": (workspace / directory / "SKILL.md").read_bytes()}
        assert actual == role_workflow.skill_files()
        assert files_digest(actual) == metadata["materialization_hash"]
    after = fixture.resolver.resolve_agent_config(agent).to_dict()
    for key in ("role", "plugins", "scope", "tool_policy", "tools", "profile_revision"):
        assert after[key] == before[key]


@pytest.mark.parametrize("runtime,directory,other", [
    ("claude-code", ".claude/skills/role", "codex"),
    ("codex", ".agents/skills/role", "claude-code"),
])
def test_role_drift_is_client_specific_and_repreparation_repairs_it(tmp_path, runtime, directory, other):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    ready = prepare(fixture, agent)
    workspace = Path(ready["workspace"])
    path = workspace / directory / "SKILL.md"
    path.write_text("unexpected local edit")
    status = fixture.resolver.resolve_agent_config(agent).data["preparation"]
    assert not status["runtimes"][runtime]["ready"]
    assert status["runtimes"][other]["ready"]
    assert directory + "/SKILL.md" in status["runtimes"][runtime]["projection_drift"]
    assert prepare(fixture, agent)["ready"]
    assert path.read_bytes() == role_workflow.skill_files()["SKILL.md"]


@pytest.mark.parametrize("name", [
    ".claude/skills/role/SKILL.md", ".agents/skills/role/SKILL.md", ".claude/commands/role.md",
])
def test_existing_short_command_is_preserved_without_any_preparation_writes(tmp_path, name):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    workspace = Path(fixture.resolver.resolve_agent_config(agent).data["workspace"])
    path = workspace / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Owner's existing command")
    with fixture.db.read() as uow:
        before = {table: uow.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("credentials", "machine_registrations")}
    with pytest.raises(DomainError) as error:
        prepare(fixture, agent, migrate_legacy=True)
    assert error.value.code == "projection_conflict"
    assert path.read_text() == "Owner's existing command"
    with fixture.db.read() as uow:
        for table, count in before.items():
            assert uow.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count


@pytest.mark.parametrize("directory", role_workflow.SKILL_DIRECTORIES)
def test_unmanaged_supporting_files_and_symlinks_are_not_adopted(tmp_path, directory):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    workspace = Path(prepare(fixture, agent)["workspace"])
    extra = workspace / directory / "notes.md"
    extra.write_text("Unmanaged notes")
    with pytest.raises(DomainError) as error:
        prepare(fixture, agent)
    assert error.value.code == "projection_conflict"
    assert extra.read_text() == "Unmanaged notes"
    extra.unlink()
    external = tmp_path / "external.md"
    external.write_text("Outside the agent")
    extra.symlink_to(external)
    with pytest.raises(DomainError) as error:
        prepare(fixture, agent)
    assert error.value.code == "projection_conflict"
    assert external.read_text() == "Outside the agent"


def test_legacy_command_added_after_preparation_is_detected(tmp_path):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    workspace = Path(prepare(fixture, agent)["workspace"])
    path = workspace / ".claude/commands/role.md"
    path.parent.mkdir(parents=True)
    path.write_text("An unrelated local command")
    status = fixture.resolver.resolve_agent_config(agent).data["preparation"]
    assert not status["runtimes"]["claude-code"]["ready"]
    assert status["runtimes"]["codex"]["ready"]
    with pytest.raises(DomainError) as error:
        prepare(fixture, agent)
    assert error.value.code == "projection_conflict"
    assert path.read_text() == "An unrelated local command"


def test_core_procedure_update_invalidates_config_and_requires_preparation(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    prepare(fixture, agent)
    before = fixture.resolver.resolve_agent_config(agent).to_dict()
    changed = {"SKILL.md": role_workflow.skill_files()["SKILL.md"] + b"\nUpdated procedure.\n"}
    monkeypatch.setattr(role_workflow, "skill_files", lambda: changed)
    after = fixture.resolver.resolve_agent_config(agent).to_dict()
    assert after["effective_config_id"] != before["effective_config_id"]
    assert not after["preparation"]["ready"]
    assert "selected_skills_changed" in after["preparation"]["projection_drift"]
    prepare(fixture, agent)
    ready = fixture.resolver.resolve_agent_config(agent).to_dict()
    assert ready["preparation"]["ready"]
    assert ready["role"] == before["role"]


def test_pre_role_registration_requires_upgrade_then_installs_without_repinning(tmp_path):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    result = prepare(fixture, agent)
    workspace = Path(result["workspace"])
    before = fixture.resolver.resolve_agent_config(agent).to_dict()
    with fixture.db.transaction() as uow:
        row = uow.repository("machine_registrations").get(result["registration_id"])
        projection = row["projection_json"]
        projection["selected_skills"].pop("role")
        for directory in role_workflow.SKILL_DIRECTORIES:
            name = directory + "/SKILL.md"
            projection["files"].pop(name)
            (workspace / name).unlink()
            (workspace / directory).rmdir()
        uow.connection.execute("UPDATE machine_registrations SET projection_json=? WHERE id=?",
                               (json.dumps(projection), row["id"]))
    assert not fixture.resolver.resolve_agent_config(agent).data["preparation"]["ready"]
    assert prepare(fixture, agent)["ready"]
    assert fixture.resolver.resolve_agent_config(agent).data["role"] == before["role"]


def test_repreparation_and_resume_preserve_queue_and_session_ownership(inbox):
    s = inbox
    row = story(s)
    subscribe(s, row)
    edit(s, row)
    result = s.runtime.application.invoke(s.fixture.ctx, "tasks_create",
        {"title": "Continue research", "assignee_ids": [s.agent.id]}, {"idempotency_key": new_id()})
    assert result.status == "ok", result.to_dict()
    pending = s.runtime.application.invoke(s.fixture.ctx, "signoffs_input_create", {"request_key": "research-direction", "form": {
        "title": "Confirm research direction", "explanation": "Which market should we prioritize?",
        "kind": "question", "selection_mode": "none", "notes_required": True}}, {"idempotency_key": new_id()})
    assert pending.status == "ok", pending.to_dict()
    assert pending.data["signoff"]["state"] == "pending"
    tables = ("tasks", "inbox_events", "inbox_deliveries", "inbox_subscriptions", "inbox_watchers", "signoffs")
    def snapshot():
        with s.fixture.db.read() as uow:
            return {table: [dict(row) for row in uow.connection.execute(f"SELECT * FROM {table}")]
                    for table in tables}
    before = snapshot()
    for _ in range(2):
        result = prepare(s.fixture, s.agent)
        assert result["ready"]
    for runtime in VERSIONS:
        for event in ("startup", "resume", "compact", "clear"):
            data = s.fixture.resolver.bootstrap(s.agent, registration_id=result["registration_id"],
                session_id=s.consumer, event=event, runtime=runtime)
            assert data["ready"]
            entry = next(row for row in data["mandatory_context"]["entries"]
                         if row["source_id"] == "core:role-workflow")
            assert entry["hash"] == content_hash(role_workflow.INSTRUCTIONS.encode())
    assert snapshot() == before


@pytest.mark.parametrize("role,mode", [("dev", "deny"), ("business-agent", "deny"),
                                      ("global-marketing", "allow"), ("ordubashy", "allow")])
def test_owner_attention_grants_follow_delegation_boundary(tmp_path, role, mode):
    fixture = Fixture(tmp_path)
    agent = fixture.create(role=role) if role in {"dev", "business-agent"} else fixture.create(role, role)
    config = fixture.resolver.resolve_agent_config(agent).to_dict()
    tools = {row["canonical_operation"]: row for row in config["tools"]}
    for op in ("signoffs_input_create", "signoffs_attention_set"):
        assert tools[op]["effective_mode"] == mode
    assert tools["signoffs_get"]["effective_mode"] == "allow"
    from bazarlyk.policy import resolve_operation_policy
    from bazarlyk.catalog.packages import load_package
    role_source = fixture.sources[role]
    manifest = load_package(role_source[1], role_source[2], "role", fixture.registry).manifest
    for op in ("email_decision_request", "email_decision_replace", "email_approval_attention_set"):
        assert resolve_operation_policy(op, manifest["default_tool_policy"], {}, fixture.registry,
                                        role_id=role).mode == mode
