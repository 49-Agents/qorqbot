from dataclasses import replace

import pytest

from foundation_support import BUSINESS, OTHER, context
from test_context_library import read, seed
from test_domain_documents import invoke, setup_documents
from bazarlyk.storage import Database


def test_classification_is_metadata_only_and_shared_by_context_reads(tmp_path):
    db, app, _ = setup_documents(tmp_path)
    identity, file = seed(db, tmp_path, body="---\nmaterial_type: document\ntags: [old]\n---\n\nExact source.\n",
        metadata={"title": "Interview", "material_type": "document", "tags": ["old"]})
    original = file.read_bytes()
    result = invoke(app, "materials_update", {"material_id": identity,
        "changes": {"material_type": "conversation", "tags": ["marketing", "customer interview"]}}, revision=1)
    assert result.status == "ok", result.to_dict()
    assert result.data["item"]["material_type"] == "conversation"
    assert result.data["item"]["origin"] == "authored"
    assert file.read_bytes() == original
    for op in ("context_list", "context_search", "context_items_list"):
        args = {"material_type": "conversation", "origin": "authored", "tag": "marketing"}
        if op == "context_search": args["query"] = "Exact"
        listed = read(app, op, args)
        assert [item["id"] for item in listed["items"]] == [identity]
        assert not read(app, op, {**args, "tag": "sales"})["items"]
    document = app.invoke(context(), "context_get", {"document_id": identity}).data["document"]
    assert document["metadata"]["material_type"] == "conversation"
    assert document["metadata"]["tags"] == ("marketing", "customer interview")
    updated = invoke(app, "context_document_update", {"document_id": identity, "body": "Authored update."}, revision=2)
    assert updated.status == "ok", updated.to_dict()
    assert updated.data["document"]["tags"] == ("marketing", "customer interview")
    assert updated.data["document"]["material_type"] == "conversation"
    stale = invoke(app, "materials_update", {"material_id": identity, "changes": {"tags": []}}, revision=1)
    assert stale.error["code"] == "revision_conflict"


def test_content_is_a_searchable_material_and_raw_uploads_stay_off_map(tmp_path):
    db, app, _ = setup_documents(tmp_path)
    content, _ = seed(db, tmp_path, kind="content", path="content/launch/post.md", body="Published example")
    upload, file = seed(db, tmp_path, kind="context_dump", body="A mixed original upload")
    original = file.read_bytes()
    assert [item["id"] for item in read(app, "context_list", {"material_type": "content"})["items"]] == [content]
    assert read(app, "context_search", {"query": "Published"})["items"][0]["origin"] == "authored"
    assert read(app, "context_get", {"document_id": content})["document"]["body"] == "Published example"
    tagged = invoke(app, "materials_update", {"material_id": upload, "changes": {"tags": ["marketing"]}}, revision=1)
    assert tagged.status == "ok", tagged.to_dict()
    assert tagged.data["item"]["origin"] == "upload" and tagged.data["item"]["raw_source"]
    assert file.read_bytes() == original
    assert upload not in {item["id"] for item in read(app, "context_items_list")["items"]}
    assert [item["id"] for item in read(app, "context_items_list", {"origin": "upload", "include_sources": True})["items"]] == [upload]
    graph = read(app, "context_graph_get")
    assert upload not in str(graph)
    bad = invoke(app, "materials_update", {"material_id": upload, "changes": {"material_type": "note"}}, revision=2)
    assert bad.error["code"] == "validation_error"


@pytest.mark.parametrize("kind", ["business_note", "agent_note", "plan", "shared_instruction"])
def test_notes_plans_and_instructions_are_not_reclassified(tmp_path, kind):
    db, app, _ = setup_documents(tmp_path)
    identity, file = seed(db, tmp_path, kind=kind, body="Leave this alone.")
    before = file.read_bytes()
    result = invoke(app, "materials_update", {"material_id": identity, "changes": {"tags": ["marketing"]}}, revision=1)
    assert result.status == "error"
    assert file.read_bytes() == before
    with db.read() as uow:
        row = uow.repository("documents", business_id=BUSINESS).get(identity)
        assert row["revision"] == 1 and "tags" not in row["metadata_json"]


def test_material_update_is_business_scoped_and_does_not_accept_body_or_origin(tmp_path):
    db, app, _ = setup_documents(tmp_path)
    identity, _ = seed(db, tmp_path)
    cross = replace(context(), business_id=OTHER, allowed_business_ids=frozenset({OTHER}))
    result = invoke(app, "materials_update", {"material_id": identity, "changes": {"tags": []}}, revision=1, ctx=cross)
    assert result.error["code"] == "not_found"
    for changes in ({"body": "Rewrite"}, {"origin": "email"}, {"tags": [" "]}, {"tags": ["x", "x"]}):
        assert invoke(app, "materials_update", {"material_id": identity, "changes": changes}, revision=1).error["code"] == "validation_error"



def test_business_agent_can_classify_without_gaining_body_edit_permission(tmp_path):
    from test_service_transports import ServiceFixture
    service = ServiceFixture(tmp_path, full=True)
    app, owner = service.runtime.application, service.fixture.ctx
    topic = invoke(app, "context_topics_create", {"title": "Customers", "slug": "customers"}, ctx=owner).data["document"]
    material = invoke(app, "context_notes_create", {"topic_id": topic["id"], "title": "Interview", "body": "Owner's words"}, ctx=owner).data["document"]
    agent = service.runtime.resolver.invocation_context(service.agent)
    result = invoke(app, "materials_update", {"material_id": material["id"], "changes": {"tags": ["marketing"]}}, revision=1, ctx=agent)
    assert result.status == "ok", result.to_dict()
    denied = invoke(app, "context_document_update", {"document_id": material["id"], "body": "Changed"}, revision=2, ctx=agent)
    assert denied.error["code"] == "policy_denied"


def test_existing_materials_are_classified_without_rewriting_files_or_notes(tmp_path, monkeypatch):
    migrations = Database.migrations()
    monkeypatch.setattr(Database, "migrations", staticmethod(lambda: migrations[:20]))
    db, _, _ = setup_documents(tmp_path)
    content, content_file = seed(db, tmp_path, kind="content", body="Original published post")
    upload, upload_file = seed(db, tmp_path, kind="context_dump", body="  Exact mixed upload\n")
    note, note_file = seed(db, tmp_path, kind="business_note", body="Owner's note")
    with db.read() as uow:
        before = {identity: uow.repository("documents", business_id=BUSINESS).get(identity) for identity in (content, upload, note)}
    originals = {file: file.read_bytes() for file in (content_file, upload_file, note_file)}
    monkeypatch.setattr(Database, "migrations", staticmethod(lambda: migrations))
    db.initialize()
    with db.read() as uow:
        after = {identity: uow.repository("documents", business_id=BUSINESS).get(identity) for identity in before}
        assert after[note] == before[note]
        for identity in (content, upload):
            assert after[identity]["content_hash"] == before[identity]["content_hash"]
            assert after[identity]["revision"] == before[identity]["revision"] + 1
        assert after[content]["metadata_json"]["material_type"] == "content"
        assert after[upload]["metadata_json"]["origin"] == "upload"
        assert after[upload]["metadata_json"]["material_type"] == "mixed"
        assert uow.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {file: file.read_bytes() for file in originals} == originals
