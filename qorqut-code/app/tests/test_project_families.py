"""Installed Project inventory families: Global routines, Dev reports, documents and their exact records."""

from __future__ import annotations

import base64
import json

from qorqut_operations.contracts import new_id

from test_project_records import (FORM, agent, call, context, context_global, host, item_of, list_items, ok,
                                  seed_project, task)


def test_a_stored_global_routine_is_a_candidate_link_and_exact_record(host):
    project = seed_project(host, name="Global Routine Court", slug="global-routine-court")
    identity, now = new_id(), "2026-10-01T00:00:00Z"
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,"
            "arguments_json,timeout_seconds,metadata_json,revision,created_at,updated_at) "
            "VALUES('global',NULL,?,'Global recap','global','root','0 0 * * *','UTC','paused','test.fixture',"
            "'{}',900,'{}',1,?,?)", (identity, now, now))
    owner = context(host, project)
    candidates = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": None, "type": "routine", "limit": 100}))["items"]
    found = item_of(candidates, "routine", identity)
    assert found["business_id"] is None and found["business_name"] == "Global"
    linked = ok(call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "routine", "id": identity, "business_id": None}}))
    assert linked["item"]["id"] == identity and linked["link"]["target_namespace"] == "global"
    detail = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "routine", "id": identity, "business_id": None}}))
    assert detail["record"]["routine"]["id"] == identity and detail["record"]["routine"]["name"] == "Global recap"
    # The routine's run history keeps its existing reader and exact routine/scope identity.
    runs = ok(call(host, owner, "routines_runs", {"scope": "global", "routine_id": identity}))
    assert runs["items"] == []
    listing = list_items(host, owner, project, section="automation", limit=100)["items"]
    assert identity in {item["id"] for item in listing}


def test_dev_reports_are_read_through_the_owner_or_scoped_agent_reader(host):
    project = seed_project(host, name="Dev Court", slug="dev-court")
    owner_ticket, agent_ticket, now = new_id(), new_id(), "2026-10-20T00:00:00Z"
    with host.db.transaction() as uow:
        for identity, reporter in ((owner_ticket, host.instance.owner), (agent_ticket, host.ordubashy)):
            uow.connection.execute(
                "INSERT INTO dev_tickets(id,title,details,kind,status,reporter_id,business_id,created_at,updated_at,revision) "
                "VALUES(?,?,?,'bug','open',?,?,?,?,1)",
                (identity, f"Report {identity[:8]}", f"Details for {identity}", reporter, project, now, now))
    owner = context(host, project)
    listing = list_items(host, owner, project, section="other", limit=100)["items"]
    assert {owner_ticket, agent_ticket} <= {item["id"] for item in listing if item["type"] == "dev_ticket"}
    detail = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "dev_ticket", "id": owner_ticket, "business_id": project}}))
    assert detail["record"]["ticket"]["id"] == owner_ticket
    assert detail["record"]["ticket"]["details"] == f"Details for {owner_ticket}"
    # The scoped agent reader is used for the configured Ordubashy, not the owner-only queue.
    agent_ctx = agent(host, project)
    agent_listing = ok(call(host, agent_ctx, "project_items_list", {
        "project_id": project, "section": "other", "limit": 100}))["items"]
    assert agent_ticket in {item["id"] for item in agent_listing if item["type"] == "dev_ticket"}
    exact = ok(call(host, agent_ctx, "project_item_get", {
        "project_id": project, "item": {"type": "dev_ticket", "id": agent_ticket, "business_id": project}}))
    assert exact["record"]["ticket"]["id"] == agent_ticket and "events" in exact["record"]
    # The target's stored business must agree with the supplied scope, never be overwritten by the caller's.
    foreign = seed_project(host, name="Dev Elsewhere", slug="dev-elsewhere")
    wrong = call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "dev_ticket", "id": owner_ticket, "business_id": foreign}})
    assert wrong.status == "error"


def test_project_documents_are_listed_and_read_by_their_own_operations(host):
    project = seed_project(host, name="Document Court", slug="document-court")
    operations = host.instance.paths.instance_root / "operations" / "projects" / "document-court"
    (operations / "hq").mkdir(parents=True, exist_ok=True)
    owner = context(host, project)
    topic = ok(call(host, owner, "context_topics_create", {"title": "Pilot", "slug": "pilot"}))
    note = ok(call(host, owner, "context_notes_create", {
        "topic_id": topic["document"]["id"], "title": "Pilot note", "body": "A synthetic pilot note."}))
    asset = ok(call(host, owner, "assets_upload", {
        "filename": "pilot.txt", "content_b64": base64.b64encode(b"pilot bytes").decode(), "group": "drafted"}))
    upload = ok(call(host, owner, "data_uploads_upload", {
        "filename": "pilot.csv", "content_b64": base64.b64encode(b"name,value\npilot,1\n").decode(),
        "notes": "Synthetic pilot data"}))
    assert note and asset and upload
    documents = [item for item in list_items(host, owner, project, section="documents", limit=100)["items"]
                 if item["type"] == "material"]
    ids = {item["id"] for item in documents}
    def created_document(created):
        return next(created[key] for key in ("document", "asset", "upload") if key in created)

    for created in (note, asset, upload):
        assert created_document(created)["id"] in ids, created
    # Each document record is the owning read, never a mutation or an unspecified reader.
    note_item = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "material", "id": created_document(note)["id"], "business_id": project}}))
    assert note_item["record"]["document"]["id"] == created_document(note)["id"]
    # The record is the canonical bounded envelope, not the dashboard one-shot shape: a reader can continue it.
    assert note_item["record"]["complete"] is True
    assert note_item["record"].get("next_cursor") is None
    assert note_item["record"]["document"]["character_offset"] == 0
    assert note_item["record"]["document"]["content_hash"]
    asset_item = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "material", "id": created_document(asset)["id"], "business_id": project}}))
    assert asset_item["record"]["asset"]["id"] == created_document(asset)["id"]
    upload_item = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "material", "id": created_document(upload)["id"], "business_id": project}}))
    assert upload_item["record"]["upload"]["id"] == created_document(upload)["id"]
    # A context_upload staging row is not an inventory document and can never reach a mutation reader.
    staged, now = new_id(), "2026-10-20T00:00:00Z"
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO documents(id,business_id,namespace,kind,path,metadata_json,source_refs_json,availability,"
            "revision,created_at,updated_at,archived) VALUES(?,?,?,'context_upload',?,?,'{}','ready',1,?,?,0)",
            (staged, project, "business", "context/upload.md", json.dumps({"title": "Staged upload"}), now, now))
    assert staged not in {item["id"] for item in list_items(host, owner, project, section="documents",
                                                            limit=100)["items"]}


def test_tables_and_context_entities_are_listed_with_their_own_reads(host):
    project = seed_project(host, name="Table Court", slug="table-court")
    owner = context(host, project)
    table_id, entity, now = new_id(), new_id(), "2026-10-20T00:00:00Z"
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO data_tables(business_id,id,title,description,columns_json,tags_json,archived,created_by,"
            "revision,created_at,updated_at) VALUES(?,?,?,?,?,'{}',0,?,1,?,?)",
            (project, table_id, "Pilot table", "A synthetic table",
             json.dumps([{"id": "name", "name": "Name", "type": "text"}]), host.instance.owner, now, now))
        uow.connection.execute(
            "INSERT INTO context_entities(business_id,id,label,url,archived,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,0,1,?,?)", (project, entity, "Pilot supplier", "https://example.invalid/pilot", now, now))
    listing = list_items(host, owner, project, section="all", limit=100)["items"]
    table_item = item_of(listing, "table", table_id)
    entity_item = item_of(listing, "entity", entity)
    assert table_item["label"] == "Pilot table"
    assert entity_item["label"] == "Pilot supplier"
    table_record = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "table", "id": table_id, "business_id": project}}))
    assert table_record["record"]["table"]["id"] == table_id
    entity_record = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "entity", "id": entity, "business_id": project}}))
    assert entity_record["record"]["item"]["label"] == "Pilot supplier"


def test_a_readable_owner_journal_is_never_project_inventory(host):
    project = seed_project(host, name="Journal Court", slug="journal-court")
    owner = context(host, project)
    journal = ok(call(host, context_global(host), "notes_create", {
        "title": "Owner private journal", "date": "2026-10-27", "body": "Synthetic owner journal."}))["document"]
    source_read = ok(call(host, context_global(host), "notes_get", {"document_id": journal["id"]}))
    assert source_read["document"]["id"] == journal["id"]
    candidates = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": None, "type": "material", "query": "Owner private journal"}))
    assert journal["id"] not in {item["id"] for item in candidates["items"]}
    listing = list_items(host, owner, project, section="documents", limit=100)["items"]
    assert journal["id"] not in {item["id"] for item in listing}
    direct = call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "material", "id": journal["id"], "business_id": None}})
    assert direct.status == "error"
    linked = call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "material", "id": journal["id"], "business_id": None}})
    assert linked.status == "error"
    # The source is untouched and still owner-readable; exclusion is a presentation boundary, not deletion.
    assert ok(call(host, context_global(host), "notes_get", {"document_id": journal["id"]}))["document"]["id"] == journal["id"]


def test_retired_routine_is_archived_history_not_a_false_retirement(host):
    project = seed_project(host, name="Retired Routine Court", slug="retired-routine-court")
    now, active_id, retired_id = "2026-10-20T00:00:00Z", new_id(), new_id()
    with host.db.transaction() as uow:
        for identity, retired in ((active_id, False), (retired_id, True)):
            uow.connection.execute(
                "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,"
                "arguments_json,timeout_seconds,metadata_json,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,'business','business','0 0 * * *','UTC','paused','test.fixture','{}',900,?,1,?,?)",
                (project, project, identity, f"Routine {identity[:8]}", json.dumps({"retired": retired}), now, now))
    owner = context(host, project)
    current = list_items(host, owner, project, section="automation", limit=100)["items"]
    assert active_id in {item["id"] for item in current}
    assert retired_id not in {item["id"] for item in current}
    history = list_items(host, owner, project, section="automation", limit=100, include_archived=True)["items"]
    assert {active_id, retired_id} <= {item["id"] for item in history}
    retired_item = next(item for item in history if item["id"] == retired_id)
    assert retired_item["resource_type"] == "routines"
    exact = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "routine", "id": retired_id, "business_id": project}}))
    assert exact["record"]["routine"]["id"] == retired_id


def test_core_agent_inventory_uses_bounded_metadata(host):
    project = seed_project(host, name="Agent Inventory Court", slug="agent-inventory-court")
    owner = context(host, project)
    page = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": None, "type": "agent", "limit": 100}))
    assert host.ordubashy in {item["id"] for item in page["items"]}
    assert all(item["type"] == "agent" for item in page["items"])
