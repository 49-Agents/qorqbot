"""Real application/storage tests for the shared relationship contract."""

import pytest

from qorqut_operations.contracts import InvocationOptions, new_id
from test_service_transports import ServiceFixture


@pytest.fixture
def service(tmp_path):
    return ServiceFixture(tmp_path, full=True)


def call(service, name, args=None, revision=None, key=None):
    return service.runtime.application.invoke(service.fixture.ctx, name, args or {},
        InvocationOptions(expected_revision=revision, idempotency_key=key or new_id())).to_dict()


def ok(result):
    assert result["status"] in {"ok", "accepted"}, result
    return result["data"]


def story(service, title="Fundraising"):
    return ok(call(service, "stories_create", {"title": title, "purpose": "Coordinate evidence"}))["story"]


def item(kind, identity):
    return {"type": kind, "id": identity}


def test_duplicate_directional_edges_custom_roles_and_timestamps(service):
    a, b = story(service), story(service, "Investor conversation")
    source, target = item("story", b["id"]), item("story", a["id"])
    args = {"source": source, "target": target, "role": "part_of", "context": "Investor discussion supports fundraising"}
    first = ok(call(service, "relationships_create", args))["relationship"]
    second = ok(call(service, "relationships_create", args))["relationship"]
    assert first["id"] != second["id"]
    assert first["created_at"] == first["updated_at"]
    assert first["source"] == source and first["target"] == target
    found = ok(call(service, "relationships_list", {"item": target, "direction": "incoming", "source_type": "story", "role": "part_of"}))["items"]
    assert {edge["id"] for edge in found} == {first["id"], second["id"]}
    assert not ok(call(service, "relationships_list", {"item": target, "direction": "outgoing"}))["items"]
    edited = ok(call(service, "relationships_update", {"relationship_id": first["id"], "changes": {
        "source": target, "target": source, "role": "inspired_by", "context": "Changed interpretation"}}, first["revision"]))["relationship"]
    assert edited["created_at"] == first["created_at"]
    assert edited["updated_at"] > first["updated_at"]
    assert not edited["predefined"] and edited["revision"] == 2
    assert ok(call(service, "relationships_get", {"relationship_id": edited["id"]}))["relationship"]["context"] == "Changed interpretation"
    stale = call(service, "relationships_remove", {"relationship_id": edited["id"]}, 1)
    assert stale["error"]["code"] == "revision_conflict"
    ok(call(service, "relationships_remove", {"relationship_id": edited["id"]}, 2))
    assert len(ok(call(service, "relationships_list"))["items"]) == 1


def test_multiple_parents_self_edges_and_idempotent_retry(service):
    a, b, c = story(service), story(service, "Parent two"), story(service, "Child")
    for target in (a, b, c):
        args = {"source": item("story", c["id"]), "target": item("story", target["id"]), "role": "part_of"}
        key = new_id()
        first = ok(call(service, "relationships_create", args, key=key))
        assert ok(call(service, "relationships_create", args, key=key)) == first
    assert len(ok(call(service, "relationships_list", {"source": item("story", c["id"])}))["items"]) == 3


def test_workflow_edges_cannot_bypass_task_tools(service):
    task = ok(call(service, "tasks_create", {"title": "Prepare introduction"}))["task"]
    args = {"source": item("task", task["id"]), "target": item("person", service.fixture.owner.id), "role": "assigned_to"}
    assert call(service, "relationships_create", args)["error"]["code"] == "workflow_required"
    edge = ok(call(service, "relationships_create", {**args, "role": "related_to"}))["relationship"]
    assert call(service, "relationships_update", {"relationship_id": edge["id"], "changes": {"role": "assigned_to"}}, 1)["error"]["code"] == "workflow_required"
    with service.fixture.db.transaction() as uow:
        uow.repository("relationships", business_id=service.fixture.business_id).update(edge["id"], {"role": "assigned_to"}, expected_revision=1)
    assert call(service, "relationships_remove", {"relationship_id": edge["id"]}, 2)["error"]["code"] == "workflow_required"


def test_item_types_and_endpoint_scope_are_validated(service):
    a = story(service)
    source = item("story", a["id"])
    missing = call(service, "relationships_create", {"source": source, "target": item("story", new_id()), "role": "mentions"})
    assert missing["error"]["code"] == "not_found"
    wrong_type = call(service, "relationships_create", {"source": source, "target": item("task", a["id"]), "role": "mentions"})
    assert wrong_type["error"]["code"] == "not_found"
    other, foreign = new_id(), new_id()
    with service.fixture.db.transaction() as uow:
        uow.repository("businesses").insert({"id": other, "name": "Other", "slug": "other", "workspace": "/other", "hq_path": "/other/hq"})
        uow.repository("stories", business_id=other).insert({"id": foreign, "title": "Private", "purpose": "Private", "state": "planned", "created_by": service.fixture.owner.id})
    assert call(service, "context_items_get", {"item": item("story", foreign)})["error"]["code"] == "not_found"
    assert call(service, "relationships_create", {"source": source, "target": item("story", foreign), "role": "mentions"})["error"]["code"] == "not_found"


def test_material_category_hides_original_uploads_by_default(service):
    uploaded = ok(call(service, "context_upload", {"body": "Exact uploaded source", "title": "Mixed source"}))["document"]
    resolved = ok(call(service, "context_items_get", {"item": item("material", uploaded["id"])}))["item"]
    assert resolved["raw_source"] and resolved["resource_type"] == "documents"
    assert uploaded["id"] not in {row["id"] for row in ok(call(service, "context_items_list", {"type": "material"}))["items"]}
    assert uploaded["id"] in {row["id"] for row in ok(call(service, "context_items_list", {"type": "material", "include_sources": True}))["items"]}


def test_roles_and_text_query(service):
    a, b = story(service), story(service, "Next round")
    ok(call(service, "relationships_create", {"source": item("story", b["id"]), "target": item("story", a["id"]), "role": "continues", "context": "Follow-up after September meeting"}))
    assert len(ok(call(service, "relationships_list", {"query": "september"}))["items"]) == 1
    assert not ok(call(service, "relationships_list", {"query": "unrelated"}))["items"]
    roles = {role["role"]: role for role in ok(call(service, "relationship_roles_list"))["roles"]}
    assert roles["manages"]["inverse_label"] == "Managed by"
    assert roles["assigned_to"]["workflow_tool"]


def test_task_assignments_and_blockers_use_only_relationships(service):
    agent = service.agent
    assignees = [service.fixture.owner.id, agent.id]
    blocker = ok(call(service, "tasks_create", {"title": "Collect evidence", "assignee_ids": assignees}))["task"]
    task = ok(call(service, "tasks_create", {"title": "Write introduction", "blocked_by": [blocker["id"]], "assignee_ids": assignees}))["task"]
    assert set(task["assignee_ids"]) == set(assignees)
    assert task["unresolved_blockers"] == [blocker["id"]]
    for identity in assignees:
        queue = ok(call(service, "tasks_list", {"assignee_id": identity}))["tasks"]
        assert {row["id"] for row in queue} == {task["id"], blocker["id"]}
    edges = ok(call(service, "relationships_list", {"source": item("task", task["id"])}))["items"]
    assert len(edges) == 3
    assert {edge["target"]["id"] for edge in edges if edge["role"] == "assigned_to"} == set(assignees)
    updated = ok(call(service, "tasks_assign", {"task_id": task["id"], "assignee_ids": []}, task["revision"]))["task"]
    assert not updated["assignee_ids"]
    assert len(ok(call(service, "relationships_list", {"source": item("task", task["id"])}))["items"]) == 1
    ok(call(service, "tasks_complete", {"task_id": blocker["id"]}, blocker["revision"]))
    assert not ok(call(service, "tasks_get", {"task_id": task["id"]}))["task"]["unresolved_blockers"]
    archived = ok(call(service, "tasks_update", {"task_id": task["id"], "changes": {"archived": True}}, updated["revision"]))["task"]
    assert archived["archived"]
    assert task["id"] not in {row["id"] for row in ok(call(service, "tasks_list"))["tasks"]}
    assert task["id"] in {row["id"] for row in ok(call(service, "tasks_list", {"include_archived": True}))["tasks"]}
    assert not ok(call(service, "relationships_list", {"source": item("task", task["id"])}))["items"]
    assert len(ok(call(service, "relationships_list", {"source": item("task", task["id"]), "include_archived": True}))["items"]) == 1
    with service.fixture.db.read() as uow:
        assert "assignee_id" not in service.fixture.db.columns("tasks")
        assert not uow.connection.execute("SELECT name FROM sqlite_master WHERE name='task_blockers'").fetchone()


def test_migration_preserves_assignments_dependencies_and_references(tmp_path, monkeypatch):
    from bazarlyk.storage.database import Database
    migrations = Database.migrations()
    monkeypatch.setattr(Database, "migrations", staticmethod(lambda: migrations[:14]))
    database = Database(tmp_path / "old.sqlite3").initialize()
    owner, business, a, b = new_id(), new_id(), "old-task-a", "old-task-b"
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "Owner"})
        uow.repository("businesses").insert({"id": business, "name": "Legacy", "slug": "legacy", "workspace": "/legacy", "hq_path": "/legacy/hq"})
        uow.repository("business_memberships", business_id=business).insert({"principal_id": owner, "is_owner": True})
        for identity in (a, b):
            uow.repository("tasks", business_id=business).insert({"id": identity, "title": identity, "status": "open", "assignee_id": owner, "created_by": owner})
        uow.connection.execute("INSERT INTO task_blockers VALUES (?,?,?)", (business, a, b))
        uow.repository("task_notes", business_id=business).insert({"id": new_id(), "task_id": a, "body": "Preserve this", "created_by": owner})
    monkeypatch.setattr(Database, "migrations", staticmethod(lambda: migrations))
    database.initialize()
    with database.read() as uow:
        edges = uow.repository("relationships", business_id=business).list()
        assert len(edges) == 3
        assert {(edge["from_id"], edge["to_id"]) for edge in edges if edge["role"] == "assigned_to"} == {(a, owner), (b, owner)}
        assert {(edge["from_id"], edge["to_id"]) for edge in edges if edge["role"] == "blocked_by"} == {(a, b)}
        assert uow.repository("task_notes", business_id=business).list()[0]["body"] == "Preserve this"
        assert not uow.connection.execute("PRAGMA foreign_key_check").fetchall()


def test_document_memberships_and_mentions_follow_edges_not_file_metadata(service):
    first = ok(call(service, "context_topics_create", {"title": "Investors", "slug": "investors"}))["document"]
    second = ok(call(service, "context_topics_create", {"title": "Customers", "slug": "customers"}))["document"]
    person = ok(call(service, "context_people_create", {"name": "Ada"}))["document"]
    material = ok(call(service, "context_notes_create", {"topic_id": first["id"], "title": "Conversation", "body": "Keep this text", "people_ids": [person["id"]]}))["document"]
    source = item("material", material["id"])
    edges = ok(call(service, "relationships_list", {"source": source}))["items"]
    membership = next(edge for edge in edges if edge["role"] == "part_of")
    ok(call(service, "relationships_create", {"source": source, "target": item("topic", second["id"]), "role": "part_of"}))
    current = ok(call(service, "context_get", {"document_id": material["id"]}))["document"]
    assert set(current["metadata"]["topic_ids"]) == {first["id"], second["id"]}
    ok(call(service, "relationships_remove", {"relationship_id": membership["id"]}, membership["revision"]))
    current = ok(call(service, "context_get", {"document_id": material["id"]}))["document"]
    assert current["metadata"]["topic_ids"] == [second["id"]] and current["body"] == "Keep this text"
    with service.fixture.db.read() as uow:
        stored = uow.repository("documents", business_id=service.fixture.business_id).get(material["id"])
        assert "topic_id" not in stored["metadata_json"] and "people" not in stored["metadata_json"]


def test_migration_unifies_document_and_email_relationships(tmp_path, monkeypatch):
    from bazarlyk.storage.database import Database
    from foundation_support import BUSINESS, database_at

    migrations = Database.migrations()
    monkeypatch.setattr(Database, "migrations", staticmethod(lambda: migrations[:17]))
    database = database_at(tmp_path)
    ids = {key: new_id() for key in ("parent", "child", "other", "person", "note", "upload", "mailbox", "conversation", "message", "projection", "agent")}
    with database.transaction() as uow:
        docs = uow.repository("documents", business_id=BUSINESS)
        for key, kind, path, metadata in (
            ("parent", "context_topic", "context/parent/_topic.md", {}),
            ("child", "context_topic", "context/elsewhere/_topic.md", {"parent_id": ids["parent"]}),
            ("other", "context_topic", "context/other/_topic.md", {}),
            ("person", "context_person", "context/_people/ada.md", {"name": "Ada"}),
            ("upload", "context_dump", "context/_dumps/source.md", {}),
            ("note", "context_note", "context/parent/note.md", {"people": ["ada"], "also_topic_ids": [ids["other"]]}),
            ("projection", "email_message", "email/message.md", {"conversation_id": ids["conversation"]}),
        ):
            docs.insert({"id": ids[key], "kind": kind, "path": path, "namespace": BUSINESS,
                "metadata_json": metadata, "content_hash": "source-hash",
                "source_refs_json": [{"resource_type": "documents", "resource_id": ids["upload"]}] if key == "note" else []})
        uow.repository("email_mailboxes", business_id=BUSINESS).insert({"id": ids["mailbox"], "address": "user@example.test"})
        uow.repository("email_conversations", business_id=BUSINESS).insert({"id": ids["conversation"], "mailbox": ids["mailbox"],
            "provider_id": "thread", "assignees_json": [ids["agent"]]})
        uow.repository("email_messages", business_id=BUSINESS).insert({"id": ids["message"], "mailbox": ids["mailbox"],
            "provider_id": "message", "conversation_id": ids["conversation"], "message_json": {"text": "Exact email"}})
        for source in ("message", "projection"):
            uow.repository("relationships", business_id=BUSINESS).insert({"id": new_id(), "from_type": "material", "from_id": ids[source],
                "to_type": "topic", "to_id": ids["parent"], "role": "concerns"})
    monkeypatch.setattr(Database, "migrations", staticmethod(lambda: migrations))
    database.initialize()
    with database.read() as uow:
        edges = uow.repository("relationships", business_id=BUSINESS).list()
        links = {(edge["from_id"], edge["to_id"], edge["role"]) for edge in edges}
        assert {
            (ids["child"], ids["parent"], "part_of"),
            (ids["note"], ids["parent"], "part_of"),
            (ids["note"], ids["other"], "concerns"),
            (ids["note"], ids["person"], "mentions"),
            (ids["note"], ids["upload"], "extracted_from"),
            (ids["conversation"], ids["agent"], "assigned_to"),
        } <= links
        assert sum(edge["from_id"] == ids["conversation"] and edge["role"] == "concerns" for edge in edges) == 2
        assert not any(edge["from_id"] in {ids["message"], ids["projection"]} for edge in edges)
        assert "assignees_json" not in database.columns("email_conversations")
        for row in uow.repository("documents", business_id=BUSINESS).list():
            assert not {"parent_id", "topic_id", "also_topic_ids", "people"} & row["metadata_json"].keys()
            assert row["content_hash"] == "source-hash"
        assert not uow.connection.execute("PRAGMA foreign_key_check").fetchall()


def test_archive_preserves_edges_content_and_children(service):
    parent = ok(call(service, "context_topics_create", {"title": "Investors", "slug": "investors"}))["document"]
    child = ok(call(service, "context_notes_create", {"topic_id": parent["id"], "title": "Call", "body": "Keep this text"}))["document"]
    target = item("topic", parent["id"])
    archived = ok(call(service, "context_items_archive", {"item": target, "archived": True}, parent["revision"]))["item"]
    assert archived["archived"]
    visible = ok(call(service, "context_items_list"))["items"]
    assert child["id"] in {row["id"] for row in visible} and parent["id"] not in {row["id"] for row in visible}
    assert len(ok(call(service, "relationships_list", {"item": target, "include_archived": True}))["items"]) == 1
    ok(call(service, "context_items_archive", {"item": target, "archived": False}, archived["revision"]))
    assert len(ok(call(service, "relationships_list", {"item": target}))["items"]) == 1
    ok(call(service, "context_items_archive", {"item": item("material", child["id"]), "archived": True}, child["revision"]))
    assert ok(call(service, "context_get", {"document_id": child["id"]}))["document"]["body"] == "Keep this text"
