from dataclasses import replace
import json

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from test_service_transports import ORIGIN, ServiceFixture


class Harness:
    """The service fixture with the helpers the journal cases call; no Context engine is involved."""

    def __init__(self, tmp_path):
        self.s = ServiceFixture(tmp_path, full=True)
        self.s.runtime.background.start = lambda: None

    @property
    def fixture(self):
        return self.s.fixture

    def ctx(self, principal=None, business=True):
        with self.fixture.db.read() as uow:
            return self.s.runtime.policy.context_for((principal or self.fixture.owner).id,
                business_id=self.fixture.business_id if business else None, review_business_id=None, uow=uow)


@pytest.fixture
def journal(tmp_path):
    h = Harness(tmp_path)
    f = h.fixture
    parent = f.create("coordinator", "ordubashy", runtime="codex", scope={
        "kind": "ecosystem", "allowed_business_ids": [f.business_id],
        "global_capabilities": ["read_global_context"]})
    with f.db.transaction() as u:
        install = u.repository("installation").get("service")
        u.repository("installation").update("service", {"management_coordinator_id": parent.id}, expected_revision=install["revision"])
    h.parent = parent
    yield h
    h.s.close()


def call(h, op, args=None, *, actor=None, revision=None, key=None, context=None):
    result = h.s.runtime.application.invoke(context or h.ctx(actor or h.parent, business=False), op, args or {},
        InvocationOptions(expected_revision=revision, idempotency_key=key or new_id()))
    return result.to_dict()


def ok(h, op, args=None, **kwargs):
    result = call(h, op, args, **kwargs)
    assert result["status"] == "ok", result
    return result["data"]


def create(h, body="A complete entry.\nSecond paragraph.\nLast sentence.", *, business=False):
    f = h.fixture
    if business:
        row = f.document("", body, "notes/old.md", f.business_id)
        with f.db.transaction() as u:
            u.repository("documents", business_id=f.business_id).update(row["id"],
                {"kind": "business_note", "metadata_json": {"title": "Old journal", "date": "2026-09-10", "statuses": {"First task": "done"}}},
                expected_revision=row["revision"])
        with f.db.read() as u:
            return u.repository("documents", business_id=f.business_id).get(row["id"])
    return ok(h, "notes_create", {"title": "Daily", "date": "2026-09-22", "body": body}, actor=f.owner)["document"]


def test_global_journal_keeps_original_business_files_and_owner_editing(journal):
    h, f = journal, journal.fixture
    old = create(h, "Original business journal body", business=True)
    original_path = f.hq / "notes/old.md"
    original = original_path.read_bytes()
    with TestClient(h.s.web) as client:
        listed = client.get("/api/root/notes").json()
        item = next(entry for entry in listed if entry["id"] == old["id"])
        assert item["content"] == original.decode() and item["date"] == "2026-09-10"
        assert item["ordubashyVisible"] is True
        assert client.get("/api/biz/widget/notes").json() == []
        assert client.post("/api/biz/widget/notes", json={"content": "Do not create here"}, headers=ORIGIN).status_code != 200
        created = client.post("/api/root/notes", json={}, headers=ORIGIN).json()
        assert created["title"] == "Daily" and created["ordubashyVisible"] is True
        changed = client.post("/api/root/notes/" + old["id"], headers=ORIGIN, json={
            "title": "Renamed", "expected_revision": item["revision"], "expected_content_hash": item["content_hash"]})
        assert changed.status_code == 200, changed.text
        assert changed.json()["content"] == original.decode()
    assert original_path.exists()
    assert not (f.root / "notes/old.md").exists()
    assert not (f.root / "notes/old.md").exists()


def test_direct_reads_are_owner_requested_full_current_and_visibility_gated(journal):
    h, f = journal, journal.fixture
    note = create(h, "Line one.\nLine two.\nLine three.")
    other = create(h, "Legacy business entry", business=True)
    listing = ok(h, "journal_entries_list")["items"]
    assert {item["document_id"] for item in listing} == {note["id"], other["id"]}
    visible = next(item for item in listing if item["document_id"] == note["id"])
    assert set(visible) == {"document_id", "title", "date"}
    read = ok(h, "journal_entry_get", {"document_id": note["id"]})["entry"]
    assert read["body"] == "Line one.\nLine two.\nLine three."
    assert read["title"] == "Daily" and read["date"] == "2026-09-22"
    assert read["document_revision"] == note["revision"] and read["content_hash"] == note["content_hash"]
    assert "review_status" not in read
    # Listing has no reviewed/unreviewed filter or marker and remains stable until the owner edits.
    assert ok(h, "journal_entries_list")["items"] == listing

    hidden = ok(h, "notes_update", {"document_id": note["id"], "metadata": {"ordubashy_visible": False}},
                actor=f.owner, revision=note["revision"])["document"]
    assert all(item["document_id"] != note["id"] for item in ok(h, "journal_entries_list")["items"])
    assert call(h, "journal_entry_get", {"document_id": note["id"]})["status"] == "error"
    assert call(h, "notes_get", {"document_id": note["id"]}, context=h.ctx(h.parent, business=False))["status"] == "error"
    # OFF is independent of owner filing; agents cannot edit even if they have generic write grants.
    for operation, args in (("notes_update", {"document_id": hidden["id"], "body": "No"}),
                            ("journal_upload_to_filer", {"document_id": hidden["id"], "expected_content_hash": hidden["content_hash"]})):
        assert call(h, operation, args, context=h.ctx(h.parent, business=False), revision=hidden["revision"])["status"] == "error"
    assert call(h, "journal_entries_list", actor=h.s.agent)["status"] == "error"
    assert call(h, "journal_entry_get", {"document_id": "00000000-0000-0000-0000-000000000001"})["status"] == "error"


def test_retired_reconciliation_handlers_are_hidden_but_old_catalog_names_fail_closed(journal):
    registry = journal.s.runtime.application.registry
    retired = ("journal_review_mark", "journal_reconcile_start", "journal_reconcile_get",
               "journal_context_find", "journal_context_object", "journal_reconcile_report")
    for name in retired:
        assert registry.known(name)
        assert not registry.metadata(name).exposure
        with pytest.raises(DomainError):
            registry.get(name)
        assert call(journal, name, {})["status"] == "error"
    assert registry.metadata("journal_entries_list").exposure == frozenset({"mcp"})
    assert registry.metadata("journal_entry_get").exposure == frozenset({"mcp"})
