"""The real composed runtime and dashboard serving the retained learning archive over its ordinary HTTP routes.

Every read goes through the actual ``DashboardCompatibility`` routes into the real candidate-only core operations over a
real synthetic store: bounded inventory pages with the original identities and statuses, two independent detail
collections with their own cursors, byte-streamed oversized pages, stale and cross-scope cursor refusal, and current
authorization. The browser check (a real Chromium against this same server, no intercepted product response) is driven
from ``test_learning_archive_runtime_browser.js``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from learning_archive_support import (_insert_candidate, COLLAPSED_ID, FILED_ID, GLOBAL_LONG_ID, GLOBAL_RICH_ID, IMPLEMENTED_ID, INVENTORY, LONG_TEXT_ID,
                                      NO_DATE_ID, REFERENCE_PAIRS, RICH_ID, build_learning_archive,
                                      write_archive_fixture)

DASHBOARD = Path(__file__).resolve().parents[2] / "biz-dashboard"
BROWSER_TEST = DASHBOARD / "tests" / "test_learning_archive_runtime_browser.js"
PLAYWRIGHT = os.environ.get("PLAYWRIGHT_MODULE", str(Path.home() / ".npm/_npx/e41f203b7505f1fb/node_modules/playwright"))


def browser_available():
    return shutil.which("node") is not None and Path(PLAYWRIGHT).is_dir() and BROWSER_TEST.is_file()


def get(host, path, headers=None):
    return httpx.get(host.origin + path, headers=headers or {}, timeout=30.0)


def business(host):
    return f"/api/biz/{host.fixture['learning_archive']['slug']}/learnings"


def collect(host, path, limit=50):
    """Every bounded inventory page, following the server's own continuation exactly as the dashboard does."""
    pages, cursor, items = [], None, []
    for _ in range(200):
        query = f"?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        response = get(host, path + query)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["read_only"] is True and page["schema_version"] == 1
        pages.append(page)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            return pages, items
    raise AssertionError("the bounded inventory never finished")


def root(host):
    return "/api/root/learnings"


def review(host, identity, query="", *, scope="business"):
    base = business(host) if scope == "business" else root(host)
    response = get(host, f"{base}/{identity}/review{query}")
    return response


def reassemble(host, identity, query, first, *, scope="business"):
    """Follow one byte stream by repeating the identical collection selection, as the browser must."""
    if first.get("complete"):
        return first
    chunks, cursor = [], first.get("next_content_cursor")
    if isinstance(first.get("content"), str):
        chunks.append(first["content"])
    assert cursor, "an incomplete page must carry its own byte continuation"
    for _ in range(1000):
        response = review(host, identity, query + ("&" if query else "?") + f"content_cursor={cursor}", scope=scope)
        assert response.status_code == 200, response.text
        chunk = response.json()
        assert chunk.get("encoding") == "json" and isinstance(chunk.get("content"), str)
        chunks.append(chunk["content"])
        if chunk["complete"]:
            return json.loads("".join(chunks))
        cursor = chunk.get("next_content_cursor")
        assert cursor
    raise AssertionError("the byte stream never completed")


def test_business_inventory_pages_keep_every_original_identity_and_status(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        archive = host.fixture["learning_archive"]
        pages, items = collect(host, f"{business(host)}/history")
        ids = [item["learning_id"] for item in items]
        assert [len(page["items"]) for page in pages] == [50, 50, 50, 50, 38], \
            "the bounded inventory needs five pages for this archive"
        assert len(ids) == len(set(ids)) == archive["inventory_total"], (len(ids), archive["inventory_total"])
        dated = [item["created"] for item in items if item["created"]]
        assert dated == sorted(dated, reverse=True), "the stored creation order is preserved without loss"
        by_id = {item["learning_id"]: item for item in items}
        assert by_id[IMPLEMENTED_ID]["state"] == "implemented"
        assert by_id[FILED_ID]["filed"] is True
        assert by_id[COLLAPSED_ID]["reconciled_into"] == "legacy-0002"
        assert by_id[LONG_TEXT_ID]["text_truncated"] is True
        assert by_id[NO_DATE_ID]["created"] is None
        assert archive["factual_id"] not in by_id, "historical inventory never lists a factual candidate"
        assert set(archive["inventory_ids"]) <= set(ids)
        for item in items:
            assert set(item) >= {"learning_id", "scope", "tag", "text", "created", "work_state", "decision", "state",
                                 "filed", "text_truncated", "reconciled_into"}
            assert "_order" not in item
        # A page size over the old aggregate cap is served by the same bounded continuation.
        wide_pages, wide_items = collect(host, f"{business(host)}/history", limit=200)
        assert [len(page["items"]) for page in wide_pages] == [200, 38]
        assert [item["learning_id"] for item in wide_items] == ids, \
            "a page size over the retired aggregate cap selects the same membership in the same order"
    finally:
        host.close()


def test_global_inventory_reads_only_the_global_scope(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        pages, items = collect(host, "/api/root/learnings/history")
        ids = [item["learning_id"] for item in items]
        assert set(ids) == {host.fixture["learning_archive"]["global_id"], GLOBAL_RICH_ID, GLOBAL_LONG_ID}
        assert all(item["scope"] == "global" for item in items)
    finally:
        host.close()


def test_detail_collections_are_independent_bounded_and_complete(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        first = review(host, RICH_ID)
        assert first.status_code == 200, first.text
        bundle = reassemble(host, RICH_ID, "", first.json())
        assert bundle["source_kind"] == "frozen_extraction_cores" and bundle["original_transcripts_included"] is False
        assert bundle["read_only"] is True
        assert "item" in bundle
        # The initial reassembled page is bounded: at most 20 rendered sources and 20 history events.
        assert len(bundle["sources"]) <= 20 and len(bundle["history"]) <= 20
        assert bundle["references_complete"] is False, "the seeded reference gaps are explicit, not silently completed"
        sources, history = list(bundle["sources"]), list(bundle["history"])
        evidence_cursor, history_cursor = bundle["next_evidence_cursor"], bundle["next_history_cursor"]
        assert evidence_cursor and history_cursor
        while evidence_cursor:
            response = review(host, RICH_ID, f"?evidence_cursor={evidence_cursor}")
            assert response.status_code == 200, response.text
            page = reassemble(host, RICH_ID, f"?evidence_cursor={evidence_cursor}", response.json())
            sources.extend(page["sources"])
            assert page["history"] == [] and "history" not in page["page_collections"]
            evidence_cursor = page["next_evidence_cursor"]
        while history_cursor:
            response = review(host, RICH_ID, f"?history_cursor={history_cursor}")
            assert response.status_code == 200, response.text
            page = reassemble(host, RICH_ID, f"?history_cursor={history_cursor}", response.json())
            history.extend(page["history"])
            assert page["sources"] == [] and "evidence" not in page["page_collections"]
            history_cursor = page["next_history_cursor"]
        pairs = {(source["run"], source["session"]) for source in sources}
        assert len(pairs) == REFERENCE_PAIRS, "both frozen runs of each session stay distinct"
        assert ("run-a", "sess-001") in pairs and ("run-b", "sess-001") in pairs
        assert len({(source["run"], source["session"]) for source in sources}) == len(pairs)
        statuses = {source["status"] for source in sources}
        assert statuses == {"available", "missing"}, "missing saved cores are explicit, never invented"
        assert any(source["core"] and source["core"]["markdown"] for source in sources)
        assert len(history) == host.fixture["learning_archive"]["history_events"]
        assert [event["created_at"] for event in history] == sorted(event["created_at"] for event in history)
        assert len({event["id"] for event in history}) == len(history)
    finally:
        host.close()


def test_nested_reconciled_evidence_is_traversed_and_byte_streams_reassemble(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        first = review(host, RICH_ID, "?history_cursor=" + "%20")
        # An empty/invalid history cursor is a stale-selection refusal, never a silent reset of both collections.
        assert first.status_code in (400, 422), first.text
        assert first.json()["error"]["code"] == "validation_error", first.text

        response = review(host, RICH_ID)
        assert response.status_code == 200
        assert response.json()["complete"] is False, "this record's selected page exceeds the direct response bound"
        bundle = reassemble(host, RICH_ID, "", response.json())
        # The nested reconciliation reaches into the nested record's own references: more pairs than the record's own.
        assert len({(source["run"], source["session"]) for source in bundle["sources"]}) >= 2
        assert bundle["read_only"] is True
        # A content cursor is bound to the exact selected page: using it on another record is refused.
        cursor = response.json()["next_content_cursor"]
        other = review(host, IMPLEMENTED_ID, f"?content_cursor={cursor}")
        assert other.status_code in (400, 422), other.text
        assert other.json()["error"]["code"] == "validation_error", other.text
    finally:
        host.close()


def test_stale_and_cross_scope_cursors_are_refused(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        first = review(host, RICH_ID)
        bundle = reassemble(host, RICH_ID, "", first.json())
        evidence_cursor = bundle["next_evidence_cursor"]
        assert evidence_cursor
        # A cursor issued for one candidate is refused on another candidate in the same scope.
        cross_candidate = review(host, IMPLEMENTED_ID, f"?evidence_cursor={evidence_cursor}")
        assert cross_candidate.status_code in (400, 422), cross_candidate.text
        assert cross_candidate.json()["error"]["code"] == "validation_error", cross_candidate.text
        # The Global archive reads its own Global record through the same candidate-only operation.
        global_first = get(host, f"/api/root/learnings/{GLOBAL_RICH_ID}/review")
        assert global_first.status_code == 200, global_first.text
        global_bundle = reassemble(host, GLOBAL_RICH_ID, "", global_first.json(), scope="global")
        global_pairs = {(source["run"], source["session"]) for source in global_bundle["sources"]}
        assert global_pairs == {("run-a", "sess-001"), ("run-b", "sess-001"), ("run-b", "sess-002")}
        # A business cursor is refused by the Global archive.
        global_response = get(host, f"/api/root/learnings/{GLOBAL_RICH_ID}/review?evidence_cursor={evidence_cursor}")
        assert global_response.status_code in (400, 422), global_response.text
        # An append after the cursor was issued invalidates that history sequence explicitly.
        history_cursor = bundle["next_history_cursor"]
        with host.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO learning_history(business_id,id,candidate_id,event,actor_id,before_json,after_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (host.fixture["learning_archive"]["business_id"], f"{RICH_ID}-late", RICH_ID, "work_note_set",
                 host.owner_id, "{}", "{}", "2026-10-30T00:00:00Z"))
        appended = review(host, RICH_ID, f"?history_cursor={history_cursor}")
        assert appended.status_code in (400, 422), appended.text
        assert appended.json()["error"]["code"] == "validation_error", appended.text
        # Reading is re-authorized on every request: a project the owner no longer belongs to is not readable.
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                                   (host.fixture["projects"]["harbor"], host.owner_id))
        assert get(host, f"{business(host)}/history").status_code == 404
        assert get(host, f"{business(host)}/{RICH_ID}/review").status_code == 404
    finally:
        host.close()


def test_the_global_archive_serves_its_own_records_and_continuations(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        pages, items = collect(host, root(host) + "/history")
        assert [item["learning_id"] for item in items] == [GLOBAL_LONG_ID, GLOBAL_RICH_ID,
                                                           host.fixture["learning_archive"]["global_id"]]
        assert pages and all(page["read_only"] for page in pages)
        first = review(host, GLOBAL_LONG_ID, scope="global")
        assert first.status_code == 200, first.text
        bundle = reassemble(host, GLOBAL_LONG_ID, "", first.json(), scope="global")
        initial = bundle
        assert bundle["read_only"] is True and bundle["source_kind"] == "frozen_extraction_cores"
        sources = list(bundle["sources"])
        evidence_cursor = bundle["next_evidence_cursor"]
        while bundle.get("next_evidence_cursor"):
            cursor = bundle["next_evidence_cursor"]
            page = review(host, GLOBAL_LONG_ID, f"?evidence_cursor={cursor}", scope="global")
            assert page.status_code == 200, page.text
            bundle = reassemble(host, GLOBAL_LONG_ID, f"?evidence_cursor={cursor}", page.json(), scope="global")
            sources.extend(bundle["sources"])
        pairs = {(source["run"], source["session"]) for source in sources}
        assert len(pairs) == 45 and {run for run, _session in pairs} == {"run-a", "run-b"}
        assert bundle["references_complete"] is True and bundle["evidence_complete"] is True
        assert len(initial["history"]) <= 20 and initial["next_history_cursor"]
        history, cursor = list(initial["history"]), initial["next_history_cursor"]
        while cursor:
            page = review(host, GLOBAL_LONG_ID, f"?history_cursor={cursor}", scope="global")
            assert page.status_code == 200, page.text
            body = page.json()
            history.extend(body["history"])
            cursor = body["next_history_cursor"]
        assert len({event["id"] for event in history}) == 25 and bundle["history_complete"] is False
        assert bundle["evidence_complete"] is True
        # The two archives bind their own scope: a business-issued cursor never reads Global and the reverse is refused.
        business_first = review(host, host.fixture["learning_archive"]["rich_id"])
        assert business_first.status_code == 200, business_first.text
        business_page = reassemble(host, host.fixture["learning_archive"]["rich_id"], "", business_first.json())
        business_cursor = business_page["next_evidence_cursor"]
        assert business_cursor
        assert review(host, GLOBAL_LONG_ID, f"?evidence_cursor={business_cursor}", scope="global").status_code in (400, 422)
        global_cursor = evidence_cursor
        assert global_cursor
        assert review(host, host.fixture["learning_archive"]["rich_id"], f"?evidence_cursor={global_cursor}").status_code in (400, 422)
    finally:
        host.close()


def test_the_same_original_id_in_both_scopes_reads_its_own_scope(tmp_path):
    """An original ID may exist in the business and in Global: every read, tag and cursor stays in its own scope."""
    host = build_learning_archive(tmp_path)
    try:
        with host.database.transaction() as uow:
            _insert_candidate(uow, None, RICH_ID, {
                "text": "Global namesake of the business record", "scope": "global", "tag": "rule", "bucket": "delta",
                "created": "2026-09-05T00:00:00Z", "run": "run-g", "evidence": {"run": "run-g", "session": "only-global"}})
        in_business = review(host, RICH_ID)
        in_global = review(host, RICH_ID, scope="global")
        assert in_business.status_code == 200 and in_global.status_code == 200, (in_business.text, in_global.text)
        # The business record's first page is oversized and streams; the Global namesake is a small direct page.
        business_body = reassemble(host, RICH_ID, "", in_business.json())
        global_body = reassemble(host, RICH_ID, "", in_global.json(), scope="global")
        assert "harbor procedure" in business_body["item"]["text"].lower()
        assert global_body["item"]["text"] == "Global namesake of the business record"
        assert {(source["run"], source["session"]) for source in global_body["sources"]} == {("run-g", "only-global")}
        assert len(business_body["sources"]) > 1 and ("run-g", "only-global") not in {
            (source["run"], source["session"]) for source in business_body["sources"]}
        # The validators are the scope-bound bytes: one scope's tag never revalidates the other's identical request.
        tags = {in_business.headers["etag"], in_global.headers["etag"]}
        assert len(tags) == 2
        assert review(host, RICH_ID, scope="global").headers["etag"] == in_global.headers["etag"], "a repeated read is stable"
        conditional = get(host, f"{root(host)}/{RICH_ID}/review", {"If-None-Match": in_business.headers["etag"]})
        assert conditional.status_code == 200 and conditional.json()["item"]["text"] == global_body["item"]["text"]
        revalidated = get(host, f"{root(host)}/{RICH_ID}/review", {"If-None-Match": in_global.headers["etag"]})
        assert revalidated.status_code == 304
        # Each inventory lists the identity once, with its own scope.
        _pages, business_items = collect(host, business(host) + "/history", limit=200)
        _pages, global_items = collect(host, root(host) + "/history")
        assert [item["scope"] for item in business_items if item["learning_id"] == RICH_ID] == ["biz"]
        assert [item["scope"] for item in global_items if item["learning_id"] == RICH_ID] == ["global"]
    finally:
        host.close()


def test_the_combined_fixture_keeps_the_installed_agentic_identity(tmp_path):
    """Building the composed host never publishes, shadows or replaces ``qorqut-agentic`` distribution metadata."""
    import importlib.metadata as metadata
    import sys

    def identity():
        found = [d for d in metadata.distributions() if (d.metadata["Name"] or "").lower().replace("_", "-") == "qorqut-agentic"]
        return sorted((d.version, str(d.locate_file(""))) for d in found)

    before = identity()
    assert [version for version, _origin in before] == ["0.2.0"], before
    host = build_learning_archive(tmp_path)
    try:
        assert identity() == before, "the fixture changed the resolved qorqut-agentic distribution"
        assert metadata.version("qorqut-agentic") == "0.2.0"
        installed = Path(before[0][1]).resolve()
        assert str(installed).endswith("site-packages"), installed
        assert not any(str(tmp_path) in entry for entry in sys.path), "the fixture must not add metadata directories to sys.path"
        import qorqut_agentic
        assert Path(qorqut_agentic.__file__).resolve().is_relative_to(installed)
        assert host.fixture["learning_archive"]["applier_agent_id"]
    finally:
        host.close()


def test_reading_the_archive_changes_nothing(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        with host.database.read() as uow:
            candidates = [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,id,candidate_json,revision,work_state,filing_json FROM learning_candidates "
                "ORDER BY business_id, id")]
            decisions = [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,candidate_id,state,changes_json FROM learning_decisions ORDER BY business_id, candidate_id")]
            history = uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0]
            receipts = uow.connection.execute("SELECT COUNT(*) FROM apply_receipts").fetchone()[0]
        collect(host, f"{business(host)}/history")
        reassemble(host, RICH_ID, "", review(host, RICH_ID).json())
        with host.database.read() as uow:
            assert [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,id,candidate_json,revision,work_state,filing_json FROM learning_candidates "
                "ORDER BY business_id, id")] == candidates
            assert [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,candidate_id,state,changes_json FROM learning_decisions "
                "ORDER BY business_id, candidate_id")] == decisions
            assert uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0] == history
            assert uow.connection.execute("SELECT COUNT(*) FROM apply_receipts").fetchone()[0] == receipts
    finally:
        host.close()


@pytest.mark.skipif(not browser_available(), reason="node/playwright or the dashboard's archive driver is unavailable")
def test_the_real_dashboard_merges_the_archive_over_its_real_routes(tmp_path):
    host = build_learning_archive(tmp_path)
    try:
        fixture = write_archive_fixture(host, tmp_path / "learning-archive.json")
        environment = {**os.environ, "LEARNING_ARCHIVE_BASE": host.origin, "LEARNING_ARCHIVE_FIXTURE": str(fixture),
                       "PLAYWRIGHT_MODULE": PLAYWRIGHT}
        completed = subprocess.run(["node", str(BROWSER_TEST)], cwd=str(BROWSER_TEST.parent), env=environment,
                                   capture_output=True, text=True, timeout=900)
        print(completed.stdout)
        print(completed.stderr)
        assert completed.returncode == 0, "the composed archive browser check failed"
    finally:
        host.close()