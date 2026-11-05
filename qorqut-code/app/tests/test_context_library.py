from dataclasses import replace
import hashlib

import pytest

from qorqut_operations.contracts import canonical_bytes, new_id
from qorqut_operations.documents import library
from foundation_support import BUSINESS, OTHER, context
from test_domain_documents import setup_documents, invoke


def seed(db, root, *, identity=None, business=BUSINESS, kind="context_note", path=None, body="", metadata=None, agent=None, namespace=None):
    identity = identity or new_id()
    path = path or f"context/{identity}.md"
    base = root if business is None else root / ("first/hq" if business == BUSINESS else "second/hq")
    file = base / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(body)
    with db.transaction() as uow:
        uow.repository("documents", business_id=business).insert({"id": identity, "kind": kind, "path": path,
            "namespace": namespace or ("business:" + business if business else "global"),
            "owning_agent_id": agent, "content_hash": hashlib.sha256(body.encode()).hexdigest(),
            "metadata_json": metadata or {"title": "Context"}})
    return identity, file


def read(app, operation, args=None, **ctx):
    result = app.invoke(replace(context(), source="mcp", **ctx), operation, args or {})
    assert result.status == "ok", result.to_dict()
    return result.data


def test_compact_browse_and_search_exclude_private_and_other_business_sources(tmp_path):
    db, app, _ = setup_documents(tmp_path)
    visible, _ = seed(db, tmp_path, body="launch public details", metadata={"title": "Launch"})
    private, _ = seed(db, tmp_path, kind="business_note", body="launch private note")
    email, _ = seed(db, tmp_path, kind="email_message", body="launch raw inbox")
    secret, _ = seed(db, tmp_path, business=OTHER, body="launch another business")
    shared, _ = seed(db, tmp_path, business=None, kind="shared_instruction", body="launch shared guidance")
    root_private, _ = seed(db, tmp_path, business=None, namespace="agent:private", body="launch personal draft")
    listed = read(app, "context_list")
    assert [x["id"] for x in listed["items"]] == [visible]
    assert "body" not in listed["items"][0]
    found = read(app, "context_search", {"query": "launch"})
    assert found["complete"] and found["matched"] == 1
    assert [x["id"] for x in found["items"]] == [visible]
    for identity in (private, email, secret, shared, root_private):
        result = app.invoke(replace(context(), source="mcp"), "context_get", {"document_id": identity})
        assert result.error["code"] == "not_found"
    shared_found = read(app, "context_search", {"query": "launch", "scope": "all"}, global_capabilities=frozenset({"read_global_context"}))
    assert {x["id"] for x in shared_found["items"]} == {visible, shared}
    denied = app.invoke(replace(context(), source="mcp"), "context_search", {"query": "launch", "scope": "shared"})
    assert denied.error["code"] == "forbidden"


def test_search_checks_nonmatching_external_edits_and_avoids_repeated_full_reads(tmp_path, monkeypatch):
    db, app, domains = setup_documents(tmp_path)
    matching, _ = seed(db, tmp_path, body="launch details")
    formerly_missing, file = seed(db, tmp_path, body="unrelated")
    store = domains.documents.store
    original, reads = store.read_bytes, []
    def observed(row, uow):
        reads.append(row["id"])
        return original(row, uow)
    monkeypatch.setattr(store, "read_bytes", observed)
    assert read(app, "context_search", {"query": "launch"})["matched"] == 1
    reads.clear()
    assert read(app, "context_search", {"query": "launch"})["complete"]
    assert reads == [matching]  # Only verify the returned excerpt; unchanged misses use stat.
    file.write_text("launch newly relevant")
    result = read(app, "context_search", {"query": "launch"})
    assert {x["id"] for x in result["items"]} == {matching, formerly_missing}
    assert not result["complete"] and "source_drift_or_unavailable" in result["incomplete_reasons"]
    assert next(x for x in result["items"] if x["id"] == formerly_missing)["metadata_stale"]
    with db.read() as uow:
        assert uow.repository("documents", business_id=BUSINESS).get(formerly_missing)["content_hash"] != hashlib.sha256(file.read_bytes()).hexdigest()


def test_bounded_rebuild_is_resumable_and_empty_partial_is_not_absence(tmp_path, monkeypatch):
    db, app, _ = setup_documents(tmp_path)
    for i in range(5):
        seed(db, tmp_path, identity=f"00000000-0000-4000-9000-{i:012d}", body="needle" if i == 4 else "unrelated")
    monkeypatch.setattr(library, "MAX_REBUILD", 2)
    first = read(app, "context_search", {"query": "needle"})
    assert not first["complete"] and first["refresh_cursor"] and not first["items"]
    second = read(app, "context_search", {"query": "needle", "refresh_cursor": first["refresh_cursor"]})
    assert not second["complete"] and second["refresh_cursor"]
    last = read(app, "context_search", {"query": "needle", "refresh_cursor": second["refresh_cursor"]})
    assert last["complete"] and last["matched"] == 1
    bad = app.invoke(replace(context(), source="mcp"), "context_search", {"query": "other", "refresh_cursor": first["refresh_cursor"]})
    assert bad.error["code"] == "validation_error"


def test_read_lines_heading_long_line_continuation_and_changed_hash(tmp_path):
    db, app, _ = setup_documents(tmp_path)
    body = "# Overview\nIntro\n## Target\n" + "a" * 15000 + "\n## Later\nFinal\n"
    identity, file = seed(db, tmp_path, body=body)
    args = {"document_id": identity, "heading": "Target", "max_chars": 1000}
    first = read(app, "context_get", args)
    assert len(first["document"]["body"]) == 1000 and first["next_cursor"]
    pieces = [first["document"]["body"]]
    cursor = first["next_cursor"]
    while cursor:
        part = read(app, "context_get", {**args, "cursor": cursor})
        pieces.append(part["document"]["body"])
        cursor = part["next_cursor"]
    assert "".join(pieces) == "## Target\n" + "a" * 15000 + "\n"
    assert read(app, "context_get", {"document_id": identity, "start_line": 1, "end_line": 2})["document"]["body"] == "# Overview\nIntro\n"
    reversed_range = app.invoke(replace(context(), source="mcp"), "context_get", {"document_id": identity, "start_line": 5, "end_line": 4})
    assert reversed_range.error["code"] == "validation_error"
    file.write_text(body.replace("Intro", "Changed"))
    stale = app.invoke(replace(context(), source="mcp"), "context_get", {**args, "cursor": first["next_cursor"]})
    assert stale.error["code"] == "validation_error"
    # Dashboard and worker projection continue to return the full document.
    dashboard = app.invoke(context(), "context_get", {"document_id": identity})
    assert len(dashboard.data["document"]["body"]) > 15000


def test_result_cursors_bind_scope_and_generation_and_results_are_bounded(tmp_path):
    db, app, _ = setup_documents(tmp_path)
    files = [seed(db, tmp_path, body="needle\n" * 10000) for _ in range(4)]
    first = read(app, "context_search", {"query": "needle", "limit": 2})
    second = read(app, "context_search", {"query": "needle", "limit": 2, "cursor": first["next_cursor"]})
    assert len(first["items"]) == len(second["items"]) == 2
    assert {x["id"] for x in first["items"]}.isdisjoint(x["id"] for x in second["items"])
    assert len(canonical_bytes(first)) < library.MAX_RESPONSE_BYTES
    files[0][1].write_text("needle changed")
    stale = app.invoke(replace(context(), source="mcp"), "context_search", {"query": "needle", "cursor": first["next_cursor"]})
    assert stale.error["code"] == "validation_error"


def test_managed_edit_refreshes_search_and_deleted_sources_disappear(tmp_path):
    _, app, _ = setup_documents(tmp_path)
    topic = invoke(app, "context_topics_create", {"title": "Product", "slug": "product"}).data["document"]
    note = invoke(app, "context_notes_create", {"title": "A note", "body": "old phrase", "topic_id": topic["id"]}).data["document"]
    assert read(app, "context_search", {"query": "old phrase"})["complete"]
    assert invoke(app, "context_document_update", {"document_id": note["id"], "body": "new phrase"}, revision=1).status == "ok"
    changed = read(app, "context_search", {"query": "new phrase"})
    assert changed["complete"] and changed["items"][0]["id"] == note["id"]
    assert not read(app, "context_search", {"query": "old phrase"})["items"]


def test_filters_apply_before_match_limit_and_unavailable_source_recovers(tmp_path, monkeypatch):
    db, app, _ = setup_documents(tmp_path)
    for i in range(5):
        seed(db, tmp_path, identity=f"00000000-0000-4000-9000-{i:012d}", body="needle", kind="context_person")
    identity, file = seed(db, tmp_path, identity="00000000-0000-4000-9000-000000000009", body="needle", kind="context_note")
    monkeypatch.setattr(library, "MAX_MATCHES", 2)
    found = read(app, "context_search", {"query": "needle", "kind": "context_note"})
    assert found["complete"] and found["matched"] == 1 and found["items"][0]["id"] == identity
    file.unlink()
    missing = read(app, "context_search", {"query": "needle", "kind": "context_note"})
    assert not missing["complete"] and not missing["items"]
    file.write_text("needle")
    assert read(app, "context_search", {"query": "needle", "kind": "context_note"})["complete"]


def test_search_cache_failure_does_not_prevent_managed_context_commit(tmp_path):
    db, app, domains = setup_documents(tmp_path)
    domains.documents.library.cache_path.mkdir()  # An unusable derived cache, not an authority failure.
    created = invoke(app, "context_topics_create", {"title": "Product", "slug": "product"})
    assert created.status == "ok", created.to_dict()
    broken = app.invoke(replace(context(), source="mcp"), "context_search", {"query": "Product"})
    assert broken.error["code"] == "context_index_unavailable"
    domains.documents.library.cache_path.rmdir()
    assert read(app, "context_search", {"query": "Product"})["items"][0]["id"] == created.data["document"]["id"]
