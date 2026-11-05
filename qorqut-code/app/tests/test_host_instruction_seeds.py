"""The three initial shared instructions are package resources of the host; registered documents stay the editable records.

No template checkout, template-root setting or sibling lookup exists: the seeds come from ``importlib.resources``. Everything
is a disposable synthetic instance; no provider, service or model is used.
"""
from importlib.resources import files
import json
import os

import pytest
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id

from qorqut_operations.document_history.text import latest
from bazarlyk.bootstrap.seed import INSTRUCTIONS, INSTRUCTION_SEEDS, instruction_seed, register_context_sources
from host_foundation_support import build_instance, create_registered_global, owner_global_context, register_sources

RESOURCES = {"agent-foundation.md", "ecosystem.md", "operating/ecosystem-agent.md"}
KEYS = {"foundation": "agent-foundation.md", "ecosystem-agent": "operating/ecosystem-agent.md", "ecosystem": "ecosystem.md"}


@pytest.fixture(autouse=True)
def no_template_setting(monkeypatch):
    monkeypatch.delenv("QORQUT_TEST_TEMPLATE_ROOT", raising=False)
    assert "QORQUT_TEST_TEMPLATE_ROOT" not in os.environ


def packaged():
    root = files("bazarlyk.bootstrap").joinpath("instructions")
    found = set()
    for item in root.iterdir():
        if item.is_dir():
            found |= {f"{item.name}/{child.name}" for child in item.iterdir() if child.is_file()}
        else:
            found.add(item.name)
    return found


def rows(instance):
    with instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(
            "SELECT * FROM documents WHERE business_id IS NULL AND kind='shared_instruction' ORDER BY path")]


def test_the_host_packages_exactly_the_three_initial_instructions_in_project_terms():
    assert packaged() == RESOURCES and {path for _, path in INSTRUCTION_SEEDS} == RESOURCES
    assert {key: path for key, path in INSTRUCTION_SEEDS} == KEYS
    texts = {path: " ".join(instruction_seed(path).decode().split()) for path in RESOURCES}
    assert all(text.strip() for text in texts.values())
    foundation, scope, ecosystem = texts["agent-foundation.md"], texts["operating/ecosystem-agent.md"], texts["ecosystem.md"]
    for text in texts.values():
        assert ("business" not in text.lower().replace("target_business_id", "").replace("write_business_strategy", "")
                and "Businesses have no" not in text)
    assert "Only the configured active Ordubashy creates a project" in foundation
    assert "empty hq/ and code/ folder" in foundation and "Ordubashy coordinates owner-authorized work, review and delegation" in foundation
    assert "no Ideas or setup-checklist workflow" in foundation
    assert "new-business setup" not in scope and "the only creator of projects" in scope
    assert "Projects have no resident agents" in foundation and "Projects have no resident agents" in ecosystem
    # The current active role model: Ordubashy, Learning Applier and reusable workers. No seed prescribes founding, marketing
    # creative or fixed departments, or describes a retired role as an active Global agent or a note author.
    assert "Global agents are Ordubashy" in scope and "Learning Applier" in scope and "reusable workers" in scope
    assert "Records of retired roles keep their attribution" in scope
    for text in (foundation, scope, ecosystem):
        lowered = text.lower()
        assert "department" not in lowered and "founding" not in lowered and "marketing creative" not in lowered
        assert "founder and global marketing" not in lowered
    for text in (foundation, scope, ecosystem):
        for stale in ("Founder / Strategist", "Founder keeps", "is Global Marketing's", "Founder writes", "Global Marketing writes",
                      "Ordubashy, Founder, Global Marketing"):
            assert stale not in text, stale
    assert "Project notes are ordinary documents of one project" in scope and "a tag grants nothing" in scope
    assert "write_business_strategy" not in scope and "strategy-notes" not in scope and "marketing-notes" not in scope
    assert "Never create or revive a project agent" in scope
    # The preserved contract text: scope, Signoffs, the owner build boundary, Context semantics and manual starts.
    for phrase in ("cannot grant authority", "An input Signoff records the owner's answer", "Deployment is separately scoped",
                   "queued is not published", "wait for the next manual start"):
        assert phrase in foundation, phrase
    assert "review routing never creates a grant" in scope and "never take a browser another operator holds" in scope


def test_registration_seeds_the_global_instructions_from_the_installed_package(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    assert "template" not in instance.paths.components and not (tmp_path / "biz-template").exists()
    registered = register_context_sources(instance.database, instance.paths)
    assert [row["metadata_json"]["bootstrap_key"] for row in registered] == list(KEYS)
    stored = rows(instance)
    assert {row["path"] for row in stored} == {f"{INSTRUCTIONS}/{path}" for path in RESOURCES}
    for row in stored:
        relative = row["path"].removeprefix(INSTRUCTIONS + "/")
        assert (instance.paths.operations / row["path"]).read_bytes() == instruction_seed(relative)
        assert row["namespace"] == "global" and row["business_id"] is None and json.loads(row["metadata_json"])["authored"] is True
        assert row["content_hash"].startswith("sha256:") or len(row["content_hash"]) == 64
    # No project is scaffolded: seeding registers no project document and writes nothing into any project folder.
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM documents WHERE business_id IS NOT NULL").fetchone()[0] == 0
    assert not (instance.paths.business_hq("first") / "context").exists()


def test_a_registered_global_agent_receives_the_packaged_instructions_through_ordinary_orientation(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    register_sources(instance, ids["first"], "first")
    agent = create_registered_global(instance, "ordubashy")
    config = instance.runtime.resolver.resolve_agent_config(Principal(agent, "agent"), business_id=ids["first"]).to_dict()
    entries = {entry["document_id"]: entry for entry in config["mandatory_context"]["entries"]}
    foundation = next(row for row in rows(instance) if row["path"].endswith("agent-foundation.md"))
    assert foundation["id"] in entries
    rendered = " ".join(config["mandatory_context"]["rendered"].split())
    assert "Only the configured active Ordubashy creates a project" in rendered
    assert "Projects have no resident agents" in rendered


def edit(instance, row, body):
    """The ordinary registered-document update operation, as the owner applies an edit to a shared instruction."""
    result = instance.runtime.application.invoke(
        owner_global_context(instance), "shared_instructions_update",
        {"document_id": row["id"], "body": body, "expected_content_hash": row["content_hash"]},
        InvocationOptions(idempotency_key=new_id(), expected_revision=row["revision"]))
    assert result.status == "ok", result.error
    return result


def version_of(instance, row):
    with instance.database.read() as uow:
        version = latest(uow.connection, "global", row["id"])
    return None if version is None else (version.version, version.content, version.author_id, version.created_at, version.reason)


def test_an_owner_edited_instruction_and_its_history_survive_reseeding_unchanged(tmp_path):
    instance, _ = build_instance(tmp_path, businesses=("first",))
    register_context_sources(instance.database, instance.paths)
    before = rows(instance)
    foundation = next(row for row in before if row["path"].endswith("agent-foundation.md"))
    assert version_of(instance, foundation)[0] == 1
    edited = "# Agent Foundation\n\nOwner-authored replacement, kept exactly.\n"
    edit(instance, foundation, edited)
    after_edit = rows(instance)
    changed = next(row for row in after_edit if row["id"] == foundation["id"])
    assert changed["content_hash"] != foundation["content_hash"] and changed["revision"] == foundation["revision"] + 1
    path = instance.paths.operations / changed["path"]
    edited_bytes = path.read_bytes()
    assert edited in edited_bytes.decode() and edited_bytes != instruction_seed("agent-foundation.md")
    history_before = version_of(instance, changed)
    assert history_before[0] == 2 and edited in history_before[1]
    again = register_context_sources(instance.database, instance.paths)
    # Same identities, hash, revision, bytes and version history; nothing was rewritten from the packaged seed.
    assert {row["path"]: row["id"] for row in again} == {row["path"]: row["id"] for row in before}
    assert rows(instance) == after_edit and path.read_bytes() == edited_bytes and version_of(instance, changed) == history_before
    assert instruction_seed("agent-foundation.md").decode() != edited


def test_conflicting_file_or_record_state_refuses_and_changes_nothing(tmp_path):
    instance, _ = build_instance(tmp_path, businesses=("first",))
    register_context_sources(instance.database, instance.paths)
    before = rows(instance)
    target = instance.paths.operations / INSTRUCTIONS / "ecosystem.md"
    target.write_text("An edit on disk that the registered record does not know.\n")
    with pytest.raises(DomainError) as refused:
        register_context_sources(instance.database, instance.paths)
    assert refused.value.code == "document_conflict" and rows(instance) == before
    assert target.read_text() == "An edit on disk that the registered record does not know.\n"
    # A record whose bootstrap key disagrees is a conflict as well, even with matching bytes.
    target.write_bytes(instruction_seed("ecosystem.md"))
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE documents SET metadata_json=json_set(metadata_json,'$.bootstrap_key','other') WHERE path=?",
                               (f"{INSTRUCTIONS}/ecosystem.md",))
    with pytest.raises(DomainError) as refused:
        register_context_sources(instance.database, instance.paths)
    assert refused.value.code == "document_conflict"


@pytest.mark.parametrize("owner_edited", [False, True], ids=["original-default", "owner-edited"])
def test_a_registered_instruction_whose_file_is_missing_is_refused_not_recreated_from_the_seed(tmp_path, owner_edited):
    instance, _ = build_instance(tmp_path, businesses=("first",))
    register_context_sources(instance.database, instance.paths)
    row = next(row for row in rows(instance) if row["path"].endswith("agent-foundation.md"))
    if owner_edited:
        edit(instance, row, "# Agent Foundation\n\nThe owner's recorded replacement.\n")
    before, version = rows(instance), version_of(instance, row)
    path = instance.paths.operations / row["path"]
    kept = path.read_bytes()
    path.unlink()
    with pytest.raises(DomainError) as refused:
        register_context_sources(instance.database, instance.paths)
    # Nothing is written, restored or refreshed: the path stays absent and the record, revision and history are untouched.
    assert refused.value.code == "document_conflict" and not path.exists()
    assert rows(instance) == before and version_of(instance, row) == version
    # Once the owner reconciles the file with the record, the same call is a no-op that keeps the exact bytes.
    path.write_bytes(kept)
    again = register_context_sources(instance.database, instance.paths)
    assert {item["path"]: item["id"] for item in again} == {item["path"]: item["id"] for item in before}
    assert rows(instance) == before and path.read_bytes() == kept and version_of(instance, row) == version


def test_only_an_unregistered_document_gets_the_packaged_bytes_and_an_authored_file_is_registered_as_found(tmp_path):
    instance, _ = build_instance(tmp_path, businesses=("first",))
    authored = instance.paths.operations / INSTRUCTIONS / "ecosystem.md"
    authored.parent.mkdir(parents=True, exist_ok=True)
    authored.write_text("Authored before the first seeding.\n")
    registered = register_context_sources(instance.database, instance.paths)
    assert len(registered) == len(RESOURCES)
    assert authored.read_text() == "Authored before the first seeding.\n"
    for relative in RESOURCES - {"ecosystem.md"}:
        assert (instance.paths.operations / INSTRUCTIONS / relative).read_bytes() == instruction_seed(relative)
    assert version_of(instance, next(row for row in rows(instance) if row["path"].endswith("/ecosystem.md")))[0] == 1
