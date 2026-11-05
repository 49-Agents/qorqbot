"""Real composed runtime plus a synthetic retained learning archive for the combined HTTP/browser qualification.

The host is the real composition from ``project_composition_support``: assembled schema and catalog, owned mutation
admission, ``create_app`` with the dashboard compatibility routes, and the combined dashboard's own static files on one
disposable loopback server. This module adds only stored learning history the retired product wrote (candidates,
decisions, append-only audit snapshots and saved extraction cores) and states every shape it relies on. No operation is
invoked to write historical procedural rows: the retained target refuses those mutations by contract.

The archive deliberately contains: original implemented/filed/collapsed identities, more than 200 source references
across two frozen runs of one session, a deep reconciliation chain, a page whose serialized size exceeds the direct
response bound, missing core metadata and gaps, an absent creation timestamp, and Global-scope records beside business
ones. All rows are synthetic and disposable under one pytest ``tmp_path``. No live data, model, provider or native
agent is used.
"""
from __future__ import annotations

import json
from pathlib import Path

from qorqut_operations.contracts import new_id

from host_foundation_support import create_registered_global
from project_composition_support import build_composed

RICH_ID = "legacy-rich-0001"
COLLAPSED_ID = "legacy-collapsed-0001"
IMPLEMENTED_ID = "legacy-implemented-0001"
FILED_ID = "legacy-filed-0001"
NO_DATE_ID = "legacy-undated-0001"
LONG_TEXT_ID = "legacy-longtext-0001"
GLOBAL_ID = "legacy-global-0001"
GLOBAL_RICH_ID = "legacy-global-rich-0001"
GLOBAL_LONG_ID = "legacy-global-long-0001"
INVENTORY = 232                       # bounded pages of 50 need five pages for these plus the special records
REFERENCE_PAIRS = 230                 # more than the retired 200-reference cap
HISTORY_EVENTS = 45                   # more than two bounded history pages
CORE_BYTES = 2400                     # 20 cores per page stays well above the 32 KB direct-response bound
NESTED_REFERENCES = 3
ACTIVE_IDS = (RICH_ID, IMPLEMENTED_ID, FILED_ID, COLLAPSED_ID, NO_DATE_ID, LONG_TEXT_ID)
SECOND_SESSIONS = ("sess-001", "sess-002")


def _created(minute):
    """A deterministic, strictly increasing stored creation stamp for the bounded inventory order."""
    return f"2026-09-{1 + minute // 1440:02d}T{(minute % 1440) // 60:02d}:{minute % 60:02d}:00Z"


def _core(run, session, *, missing=False):
    if missing:
        return None
    body = f"Frozen extraction core {run}/{session}. " + ("Evidence line. " * 150)
    return {"markdown": body, "source_status": "complete", "run": run, "session": session}


def _reference_list():
    """230 distinct (run, session) pairs: both frozen runs carry every session, so session alone is never an identity."""
    references = []
    for index in range(REFERENCE_PAIRS):
        run = "run-a" if index % 2 == 0 else "run-b"
        references.append({"run": run, "session": f"sess-{index // 2 + 1:03d}"})
    # One exact duplicate pair (identity dedupe) and metadata that must become an explicit gap, not a dropped entry.
    references.append({"run": "run-a", "session": "sess-001"})
    references.append({"run": "run-a"})
    references.append("not-reference-metadata")
    return references


def _rich_candidate():
    references = _reference_list()
    nested = {
        "text": "Nested earlier record reached through reconciliation",
        "scope": "biz", "tag": "rule", "bucket": "delta", "created": "2026-08-01T00:00:00Z",
        "run": "run-a", "evidence": {"run": "run-a", "session": "sess-001"},
        "evidence_more": [{"run": "run-b", "session": "sess-002"}, {"run": "run-a", "session": "sess-003"}],
        "reconciliations": [{"kind": "duplicate", "earlier": {
            "text": "Deeply nested retained record", "scope": "biz", "tag": "rule", "bucket": "delta",
            "created": "2026-07-01T00:00:00Z",
            "evidence": {"run": "run-b", "session": "sess-004"}}}],
    }
    return {
        "text": "Retained harbor procedure with a long frozen evidence trail <literal> & \"quoted\"",
        "scope": "biz", "tag": "rule", "bucket": "delta", "created": "2026-09-20T00:00:00Z",
        "run": "run-a", "evidence": references[0], "evidence_more": references[1:],
        "reconciliations": [{"kind": "duplicate", "automatic": True, "earlier": nested}],
    }


def _global_long_candidate():
    """A Global record whose evidence and history both need more than one bounded page (and a byte-streamed page)."""
    references = [{"run": "run-a" if index % 2 == 0 else "run-b", "session": f"sess-{index + 1:03d}"}
                  for index in range(45)]
    return {"text": "Global retained procedure with a long frozen evidence trail", "scope": "global", "tag": "rule",
            "bucket": "delta", "created": "2026-09-04T00:00:00Z", "run": "run-a",
            "evidence": references[0], "evidence_more": references[1:]}


def _insert_candidate(uow, business, identity, candidate, *, work_state=None, filing=None, updated=None):
    uow.connection.execute(
        "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at,work_state,filing_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (business, identity, json.dumps(candidate), updated or "2026-09-21T00:00:00Z",
         updated or "2026-09-21T00:00:00Z", work_state, json.dumps(filing or {})))


def _insert_decision(uow, business, identity, state, owner, *, note=None):
    uow.connection.execute(
        "INSERT INTO learning_decisions(business_id,candidate_id,state,feedback,changes_json,decided_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (business, identity, state, note, "{}", owner, "2026-09-21T01:00:00Z", "2026-09-21T01:00:00Z"))


def _insert_history(uow, business, identity, count, actor, *, prefix="event"):
    rows = []
    for index in range(count):
        rows.append({
            "id": f"{identity}-{prefix}-{index:03d}",
            "candidate_id": identity, "event": "learnings_review" if index % 2 == 0 else "work_done",
            "actor_id": actor,
            "before_json": json.dumps({"revision": index, "state": "open", "bucket": "delta", "tag": "rule"}),
            "after_json": json.dumps({"revision": index + 1, "state": "accepted", "bucket": "delta",
                                      "tag": "rule", "note": f"recorded step {index}"}),
            "created_at": f"2026-09-{22 + index // 60:02d}T{index % 60:02d}:00:00Z"})
    uow.connection.executemany(
        "INSERT INTO learning_history(business_id,id,candidate_id,event,actor_id,before_json,after_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(business, row["id"], row["candidate_id"], row["event"], row["actor_id"], row["before_json"],
          row["after_json"], row["created_at"]) for row in rows])


def _insert_runs(uow, business):
    for run in ("run-a", "run-b"):
        cores = {}
        for index in range(REFERENCE_PAIRS // 2):
            session = f"sess-{index + 1:03d}"
            if session in {"sess-005", "sess-017"}:
                continue                                    # a missing saved core: an explicit gap, never invented
            cores[session] = _core(run, session)
        uow.connection.execute(
            "INSERT INTO learning_runs(business_id,id,result_json,created_at) VALUES (?,?,?,?)",
            (business, run, json.dumps({"id": run, "cores": cores, "session_ids": sorted(cores)}),
             "2026-09-20T00:00:00Z"))


def _delta(text, **extra):
    return {"text": text, "scope": "biz", "tag": "rule", "bucket": "delta", "created": "2026-09-01T00:00:00Z", **extra}


def seed_learning_archive(host):
    """Seed the retained archive next to the ordinary factual candidate the composed fixture already has."""
    business = host.fixture["projects"]["harbor"]
    owner = host.owner_id
    created = [_created(index) for index in range(INVENTORY)]
    with host.database.transaction() as uow:
        for index in range(INVENTORY):
            identity = f"legacy-{index:04d}"
            _insert_candidate(uow, business, identity,
                              _delta(f"Retained rule {index:04d}", created=created[index]))
            if index % 4 == 0:
                _insert_decision(uow, business, identity, "accepted", owner, note=f"Owner accepted {index:04d}")
        _insert_runs(uow, business)

        # Original identities in the states the retired product left them in.
        _insert_candidate(uow, business, IMPLEMENTED_ID, _delta("Implemented harbor rule"),
                          work_state="implemented")
        _insert_decision(uow, business, IMPLEMENTED_ID, "accepted", owner, note="Owner accepted the implemented rule")
        _insert_candidate(uow, business, FILED_ID, _delta("Filed harbor rule"),
                          filing={"upload_id": "legacy-upload", "path": "context/legacy/filed.md"})
        _insert_decision(uow, business, FILED_ID, "accepted", owner, note="Owner filed this record")
        _insert_candidate(uow, business, COLLAPSED_ID,
                          _delta("Collapsed harbor rule", reconciled_into="legacy-0002"))
        _insert_decision(uow, business, COLLAPSED_ID, "rejected", owner, note="Superseded by a later record")
        _insert_candidate(uow, business, NO_DATE_ID,
                          {"text": "Undated retained rule", "scope": "biz", "tag": "rule", "bucket": "delta"})
        _insert_candidate(uow, business, LONG_TEXT_ID, _delta("Long retained rule " + "x" * 900))

        # The complete evidence/history record: 230 references over two runs, a nested reconciliation chain, a byte-
        # streamed page and a history longer than one bounded page.
        _insert_candidate(uow, business, RICH_ID, _rich_candidate())
        _insert_decision(uow, business, RICH_ID, "accepted", owner, note="Owner kept the harbor procedure")
        _insert_history(uow, business, RICH_ID, HISTORY_EVENTS, owner)

        # A Global-scope historical record and a Global rich record: the root archive reads only the Global scope.
        _insert_candidate(uow, None, GLOBAL_ID,
                          {"text": "Global retained rule", "scope": "global", "tag": "rule", "bucket": "delta",
                           "created": "2026-09-02T00:00:00Z"})
        _insert_candidate(uow, None, GLOBAL_RICH_ID,
                          {"text": "Global retained rule with evidence", "scope": "global", "tag": "rule",
                           "bucket": "delta", "created": "2026-09-03T00:00:00Z",
                           "run": "run-a", "evidence": {"run": "run-a", "session": "sess-001"},
                           "evidence_more": [{"run": "run-b", "session": "sess-001"},
                                             {"run": "run-b", "session": "sess-002"}]})
        _insert_candidate(uow, None, GLOBAL_LONG_ID, _global_long_candidate())
        _insert_history(uow, None, GLOBAL_LONG_ID, 25, owner)
        _insert_runs(uow, None)

        # A factual candidate in the same scope must never appear in the historical inventory.
        _insert_candidate(uow, business, "factual-context-0001",
                          {"text": "The launch date is the twelfth", "scope": "biz", "tag": "decision",
                           "bucket": "context", "created": "2026-09-25T00:00:00Z"})
    host.fixture["learning_archive"] = {
        "business_id": business,
        "slug": "harbor-light",
        "inventory_total": INVENTORY + len(ACTIVE_IDS),
        "inventory_ids": [f"legacy-{index:04d}" for index in range(INVENTORY)] + list(ACTIVE_IDS),
        "rich_id": RICH_ID,
        "implemented_id": IMPLEMENTED_ID,
        "filed_id": FILED_ID,
        "collapsed_id": COLLAPSED_ID,
        "no_date_id": NO_DATE_ID,
        "long_text_id": LONG_TEXT_ID,
        "global_id": GLOBAL_ID,
        "global_rich_id": GLOBAL_RICH_ID,
        "global_long_id": GLOBAL_LONG_ID,
        "reference_pairs": REFERENCE_PAIRS,
        "history_events": HISTORY_EVENTS,
        "factual_id": "factual-context-0001",
    }


def build_learning_archive(tmp_path):
    """The composed real host with the retained archive seeded and its HTTP server running on loopback."""
    host = build_composed(tmp_path)
    seed_learning_archive(host)
    # The actual Learning Applier is registered through the real catalog and ``agents_create`` so the dashboard's owning
    # Context pane resolves it. Registration starts no native session and no model turn.
    host.fixture["learning_archive"]["applier_agent_id"] = create_registered_global(host.instance, "learning-applier")
    return host


def write_archive_fixture(host, path):
    Path(path).write_text(json.dumps(host.fixture, indent=2) + "\n")
    return Path(path)