"""Operational source documents at the Operations boundary: framing, scopes, journal visibility, source plans and the library."""

from dataclasses import replace
import hashlib

from qorqut_operations.contracts import canonical_bytes, new_id
from qorqut_operations.documents import library
from qorqut_operations.documents.business_notes import KIND as PROJECT_NOTE
from qorqut_operations.documents.serialization import split_document
from documents_support import AGENT, BUSINESS, COORDINATOR, HUMAN, OTHER, READER, DocumentsHost


def mcp(ctx):
    return replace(ctx, source="mcp")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def project_note(host, title="Plan", body="Hello\n", **extra):
    return host.ok("notes_create", {"title": title, "body": body, "target_business_id": BUSINESS, **extra})["document"]


def test_a_project_note_is_a_registered_file_with_matching_hash_and_framing(tmp_path):
    host = DocumentsHost(tmp_path)
    document = project_note(host)
    assert document["kind"] == PROJECT_NOTE and document["availability"] == "ready" and document["revision"] == 1
    row, data = host.document(document["id"]), host.file(document["id"]).read_bytes()
    assert row["content_hash"] == digest(data) and row["namespace"] == BUSINESS
    metadata, body = split_document(data)
    assert body == "Hello\n" and metadata["title"] == "Plan" and metadata["author_id"] == HUMAN
    assert row["metadata_json"]["title"] == "Plan"


def test_notes_live_in_their_own_scope_and_root(tmp_path):
    host = DocumentsHost(tmp_path)
    mine = host.ok("notes_create", {"title": "Mine", "body": "private\n"}, host.agent())["document"]
    journal = host.ok("notes_create", {"title": "Day", "body": "dear diary\n"}, host.owner(None))["document"]
    project = project_note(host)
    assert (mine["kind"], mine["agent_id"], mine["namespace"]) == ("agent_note", AGENT, "global")
    assert mine["path"] == f"agents/{AGENT}/files/notes/" + mine["path"].rsplit("/", 1)[1]
    assert host.file(mine["id"]).parent == host.ops_root / "agents" / AGENT / "files" / "notes"
    assert (journal["kind"], journal["namespace"], journal["business_id"]) == ("global_note", "global", None)
    assert host.file(journal["id"]).parent == host.ops_root / "notes"
    assert host.file(project["id"]).parent == tmp_path / "first" / "hq" / "notes"
    # An agent sees its own notes, not the owner's journal or a project's notes.
    assert [item["id"] for item in host.ok("notes_list", {}, host.agent())["items"]] == [mine["id"]]
    assert [item["id"] for item in host.ok("notes_list", {}, host.owner(None))["items"]] == [journal["id"]]
    assert [item["id"] for item in host.ok("notes_list", {"target_business_id": BUSINESS})["items"]] == [project["id"]]
    assert host.refused("notes_list", {}, host.owner())["code"] == "validation_error"


def test_the_journal_belongs_to_the_installation_owner_alone(tmp_path):
    host = DocumentsHost(tmp_path)
    assert host.refused("notes_create", {"title": "Day", "body": "x\n"}, host.stranger(None))["code"] in {"forbidden", "policy_denied"}
    assert host.refused("notes_create", {"title": "Day", "body": "x\n"}, host.owner())["code"] == "scope_required"
    assert not host.query("SELECT id FROM documents")
    entry = host.ok("notes_create", {"title": "Day", "body": "x\n"}, host.owner(None))["document"]
    assert entry["metadata"]["ordubashy_visible"] is True
    assert host.refused("notes_get", {"document_id": entry["id"]}, host.stranger(None))["code"] in {"forbidden", "policy_denied", "not_found"}
    assert host.ok("notes_get", {"document_id": entry["id"]}, host.owner(None))["document"]["id"] == entry["id"]


def test_note_updates_keep_the_framing_and_refuse_stale_revisions_and_hashes(tmp_path):
    host = DocumentsHost(tmp_path)
    note = project_note(host)
    updated = host.ok("notes_update", {"document_id": note["id"], "body": "Changed\n", "target_business_id": BUSINESS,
                                       "expected_content_hash": note["content_hash"]}, expected_revision=1)["document"]
    assert updated["revision"] == 2 and updated["content_hash"] != note["content_hash"]
    metadata, body = split_document(host.file(note["id"]).read_bytes())
    assert body == "Changed\n" and metadata["content_owner"] == HUMAN and metadata["author_id"] == HUMAN
    stale = host.refused("notes_update", {"document_id": note["id"], "body": "Again\n", "target_business_id": BUSINESS},
                         expected_revision=1)
    assert stale["code"] == "revision_conflict"
    wrong = host.refused("notes_update", {"document_id": note["id"], "body": "Again\n", "target_business_id": BUSINESS,
                                          "expected_content_hash": note["content_hash"]}, expected_revision=2)
    assert wrong["code"] in {"revision_conflict", "document_conflict"}
    assert host.refused("notes_update", {"document_id": note["id"], "body": "x\n"}, expected_revision=2)["code"] == "validation_error"
    assert split_document(host.file(note["id"]).read_bytes())[1] == "Changed\n"


def test_archiving_keeps_the_file_and_leaves_the_listing(tmp_path):
    host = DocumentsHost(tmp_path)
    note = project_note(host)
    host.ok("notes_delete", {"document_id": note["id"], "target_business_id": BUSINESS}, expected_revision=1)
    assert host.document(note["id"])["archived"] == 1 and host.file(note["id"]).exists()
    assert host.ok("notes_list", {"target_business_id": BUSINESS})["items"] == []


def test_visibility_belongs_to_journal_entries_only(tmp_path):
    host = DocumentsHost(tmp_path)
    note = project_note(host)
    refused = host.refused("notes_update", {"document_id": note["id"], "metadata": {"ordubashy_visible": False},
                                            "target_business_id": BUSINESS}, expected_revision=1)
    assert refused["code"] == "validation_error"
    entry = host.ok("notes_create", {"title": "Day", "body": "x\n"}, host.owner(None))["document"]
    hidden = host.ok("notes_update", {"document_id": entry["id"], "metadata": {"ordubashy_visible": False}},
                     host.owner(None), expected_revision=1)["document"]
    assert hidden["metadata"]["ordubashy_visible"] is False


def test_journal_reads_follow_the_coordinator_the_visibility_and_the_registered_hash(tmp_path):
    host = DocumentsHost(tmp_path)
    shown = host.ok("notes_create", {"title": "Shown", "body": "open\n"}, host.owner(None))["document"]
    hidden = host.ok("notes_create", {"title": "Hidden", "body": "closed\n"}, host.owner(None))["document"]
    host.ok("notes_update", {"document_id": hidden["id"], "metadata": {"ordubashy_visible": False}}, host.owner(None), expected_revision=1)
    coordinator = host.agent(COORDINATOR, None)
    listed = host.ok("journal_entries_list", {}, coordinator)
    assert [item["document_id"] for item in listed["items"]] == [shown["id"]] and "not a review or filing marker" in listed["instruction"]
    entry = host.ok("journal_entry_get", {"document_id": shown["id"]}, coordinator)["entry"]
    assert entry["body"] == "open\n" and entry["document_revision"] == 1 and entry["title"] == "Shown"
    # The hidden entry and a stale or unknown identifier are indistinguishable.
    for identifier in (hidden["id"], "00000000-0000-4000-8000-0000000000ff"):
        assert host.refused("journal_entry_get", {"document_id": identifier}, coordinator)["message"] == "Journal entry is unavailable"
    # Only the configured coordinator, at Global, never delegated.
    assert host.refused("journal_entries_list", {}, host.agent(AGENT, None))["code"] in {"forbidden", "policy_denied"}
    # (The engine already treats a Global-only operation as Global whatever project the caller selected.)
    other = host.agent(READER, None)
    assert host.refused("journal_entries_list", {}, other)["message"].startswith("Direct journal reads belong")
    assert host.refused("journal_entry_get", {"document_id": shown["id"]}, other)["message"].startswith("Direct journal reads belong")
    assert host.refused("journal_entries_list", {}, replace(coordinator, home_business_id=BUSINESS))["code"] == "forbidden"
    delegated = replace(coordinator, delegation_binding_id=new_id(), delegation_assignment_id=new_id(), delegation_generation=1)
    assert host.refused("journal_entries_list", {}, delegated)["code"] in {"forbidden", "policy_denied"}
    # A file edited outside the registry is never served.
    path = host.file(shown["id"])
    path.write_bytes(path.read_bytes() + b"tampered\n")
    assert host.refused("journal_entry_get", {"document_id": shown["id"]}, coordinator)["code"] == "revision_conflict"


def test_people_topics_and_notes_write_registered_documents_and_the_file_inventory(tmp_path):
    host = DocumentsHost(tmp_path)
    topic = host.ok("context_topics_create", {"title": "Sales", "slug": "sales"})["document"]
    person = host.ok("context_people_create", {"name": "Ada Lovelace"})["document"]
    note = host.ok("context_notes_create", {"topic_id": topic["id"], "title": "Call", "body": "Met Ada.\n",
                                            "people_ids": [person["id"]]})["document"]
    assert (topic["path"], person["path"]) == ("context/sales/_topic.md", "context/_people/ada-lovelace.md")
    assert note["path"].startswith("context/sales/") and note["kind"] == "context_note"
    index = next(row for row in host.query("SELECT * FROM documents") if row["path"] == "context/INDEX.md")
    text = split_document(host.file(index["id"]).read_bytes())[1]
    assert index["kind"] == "context_index" and "[Sales](sales/_topic.md)" in text and "Ada Lovelace" in text
    shown = host.ok("context_get", {"document_id": note["id"]})["document"]
    assert shown["metadata"]["topic_id"] == topic["id"] and shown["metadata"]["people"] == [person["id"]]
    edges = {(edge["role"], edge["to_type"]) for edge in host.query("SELECT * FROM relationships")}
    assert {("part_of", "topic"), ("mentions", "person")} <= edges
    moved = host.ok("context_topics_move", {"document_id": topic["id"], "slug": "revenue"}, expected_revision=1)
    assert moved["document"]["path"] == "context/revenue/_topic.md"
    assert host.document(note["id"])["path"].startswith("context/revenue/")


def test_a_context_document_update_stays_in_the_selected_project(tmp_path):
    host = DocumentsHost(tmp_path)
    topic = host.ok("context_topics_create", {"title": "Sales", "slug": "sales"})["document"]
    updated = host.ok("context_document_update", {"document_id": topic["id"], "body": "# Sales\n\nNew.\n"}, expected_revision=1)["document"]
    assert updated["revision"] == 2 and "New." in split_document(host.file(topic["id"]).read_bytes())[1]
    elsewhere = host.refused("context_document_update", {"document_id": topic["id"], "body": "x\n"}, host.owner(OTHER), expected_revision=2)
    assert elsewhere["code"] in {"not_found", "forbidden"}


def test_the_library_lists_gets_and_searches_the_selected_scope(tmp_path):
    host = DocumentsHost(tmp_path)
    topic = host.ok("context_topics_create", {"title": "Sales", "slug": "sales"})["document"]
    note = host.ok("context_notes_create", {"topic_id": topic["id"], "title": "Call", "body": "# Call\n\nThe zebra budget is approved.\n"})["document"]
    ctx = mcp(host.owner())
    listed = host.ok("context_list", {}, ctx)
    assert {item["id"] for item in listed["items"]} >= {topic["id"], note["id"]}
    assert all(item["filing_status"] is None and "filing" not in item for item in listed["items"])
    found = host.ok("context_search", {"query": "zebra"}, ctx)
    assert [item["id"] for item in found["items"]] == [note["id"]] and "zebra" in found["items"][0]["excerpt"]
    assert found["complete"] is True
    got = host.ok("context_get", {"document_id": note["id"]}, ctx)
    assert got["complete"] is True and "zebra" in got["document"]["body"] and got["document"]["headings"][0]["title"] == "Call"
    assert (tmp_path / "state.sqlite3").parent.joinpath("context-search.sqlite3").exists()


def test_the_presentation_callback_is_applied_inside_every_read_and_before_the_response_is_sized(tmp_path):
    def present(item, row, ctx, uow):
        return {**item, "published": row["kind"], "weight": "x" * 20000}
    host = DocumentsHost(tmp_path, present_item=present)
    topic = host.ok("context_topics_create", {"title": "Sales", "slug": "sales"})["document"]
    notes = [host.ok("context_notes_create", {"topic_id": topic["id"], "title": f"Call {n}", "body": f"zebra {n}\n"})["document"] for n in range(4)]
    ctx = mcp(host.owner())
    got = host.ok("context_get", {"document_id": notes[0]["id"]}, ctx)["document"]
    assert got["published"] == "context_note"
    listed = host.ok("context_list", {}, ctx)
    searched = host.ok("context_search", {"query": "zebra"}, ctx)
    for page in (listed, searched):
        assert page["items"] and all(item["published"] == item["kind"] for item in page["items"])
        assert len(canonical_bytes(page)) < library.MAX_RESPONSE_BYTES
    # Four 20 KB items cannot fit one page: the presentation was counted, then the page sliced.
    assert len(listed["items"]) < 5 and listed["next_cursor"] and searched["next_cursor"]


def test_a_body_cursor_survives_a_metadata_only_change_and_not_a_body_change(tmp_path):
    host = DocumentsHost(tmp_path)
    body = "".join(f"line {n} " + "word " * 40 + "\n" for n in range(60))
    document = host.write("context_note", "context/long.md", body, {"title": "Long"})
    ctx = mcp(host.owner())
    first = host.ok("context_get", {"document_id": document["id"], "max_chars": 500}, ctx)
    assert first["next_cursor"] and not first["complete"]
    # A filing completion rewrites frontmatter only: the revision and the file digest change, the body does not.
    changed = host.write("context_note", document_id=document["id"], metadata={"status": "filed"})
    assert changed["revision"] == 2 and changed["content_hash"] != document["content_hash"]
    second = host.ok("context_get", {"document_id": document["id"], "max_chars": 500, "cursor": first["next_cursor"]}, ctx)
    assert second["document"]["character_offset"] == len(first["document"]["body"]) and not second["document"]["metadata_stale"]
    joined = first["document"]["body"] + second["document"]["body"]
    assert body.startswith(joined)
    # A different body makes the old cursor invalid.
    host.write("context_note", document_id=document["id"], body=body + "more\n")
    stale = host.refused("context_get", {"document_id": document["id"], "max_chars": 500, "cursor": first["next_cursor"]}, ctx)
    assert stale["code"] == "validation_error"


def test_one_cache_at_the_existing_location_is_reused_by_every_read(tmp_path):
    host = DocumentsHost(tmp_path)
    document = host.write("context_note", "context/a.md", "alpha bravo\n", {"title": "A"})
    ctx = mcp(host.owner())
    assert host.ok("context_search", {"query": "bravo"}, ctx)["items"][0]["id"] == document["id"]
    assert host.library.cache_path == tmp_path / "context-search.sqlite3"
    assert sorted(path.name for path in tmp_path.glob("*.sqlite3")) == ["context-search.sqlite3", "state.sqlite3"]
