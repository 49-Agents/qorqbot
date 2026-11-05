"""Bounded, resumable candidate evidence and history reads for owner-led learning review.

The subject is the real registry, operation layer and database of a synthetic host: candidate-only review pages, two
independent collection cursors (frozen evidence and append-only history), byte continuation of one selected page, and
the explicit stale/cross-scope cursor refusals. No model, provider, HTTP route or operational data is used.
"""
import base64
import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import InvocationOptions, new_id, plain, utc_now
from host_foundation_support import build_instance, owner_global_context

BUSINESS = "first"
SECOND = "second"


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=(BUSINESS, SECOND))
    instance.runtime.freeze_registry()
    yield SimpleNamespace(instance=instance, ids=ids, database=instance.database)
    instance.runtime.release_instance()


def call(host, name, arguments, ctx=None):
    return host.instance.runtime.application.invoke(ctx or business_owner(host), name, arguments,
                                                   InvocationOptions(idempotency_key=new_id()))


def data(host, name, arguments, ctx=None):
    result = call(host, name, arguments, ctx)
    assert result.status == "ok", (name, result.to_dict())
    return plain(result.data)      # the same fresh JSON value a transport receives


def business_owner(host, business=BUSINESS):
    return host.instance.human(host.ids[business])


def global_owner(host):
    return owner_global_context(host.instance)


def scope(host, business=BUSINESS):
    """The selected business id (``None`` for Global) from the fixture's registered slug."""
    return None if business == "global" else host.ids[business]


def write(host, callback, business=BUSINESS):
    with host.database.transaction() as uow:
        return callback(uow, scope(host, business))


def insert_candidate(host, identity, value, business=BUSINESS, **columns):
    now = utc_now()
    return write(host, lambda uow, biz: uow.repository("learning_candidates", business_id=biz).insert(
        {"id": identity, "candidate_json": value, "created_at": now, "updated_at": now, **columns}), business)


def insert_run(host, run, cores, business=BUSINESS):
    return write(host, lambda uow, biz: uow.repository("learning_runs", business_id=biz).insert(
        {"id": run, "result_json": {"cores": cores}, "created_at": utc_now()}), business)


def insert_decision(host, candidate_id, values, business=BUSINESS):
    now = utc_now()
    return write(host, lambda uow, biz: uow.repository("learning_decisions", business_id=biz).insert(
        {"candidate_id": candidate_id, "state": values.pop("state", "open"), "changes_json": values.pop("changes_json", {}),
         "decided_by": host.instance.owner, "created_at": now, "updated_at": now, **values}), business)


def insert_history(host, identity, events, business=BUSINESS):
    """``events`` are (id, after_json, created_at) triples for one candidate's append-only history."""
    def seed(uow, business_id):
        for event_id, after, created in events:
            uow.repository("learning_history", business_id=business_id).insert({
                "id": event_id, "candidate_id": identity, "event": "learnings_review",
                "actor_id": host.instance.owner, "before_json": {}, "after_json": after, "created_at": created})
    return write(host, seed, business)


def evidence(session, run=None, **extra):
    return {"session": session, **({"run": run} if run else {}), **extra}


def collect(host, learning_id, *, collection, cursor, limit=100, ctx=None):
    """Follow one collection cursor to its end; returns every page and the merged entries."""
    pages, entries = [], []
    while cursor:
        page = data(host, "learnings_review_get", {"learning_id": learning_id, f"{collection}_cursor": cursor}, ctx)
        assert page["page_collections"] == [collection], page["page_collections"]
        pages.append(page)
        entries.extend(page["sources" if collection == "evidence" else "history"])
        cursor = page[f"next_{collection}_cursor"]
        assert len(pages) < limit, "the collection cursor did not terminate"
    return pages, entries


def reassemble(host, learning_id, first, ctx=None):
    """Follow one byte continuation with the same (empty) collection selection and decode the page."""
    cursor, chunks, requests = first["next_content_cursor"], [], 0
    while cursor:
        page = data(host, "learnings_review_get", {"learning_id": learning_id, "content_cursor": cursor}, ctx)
        assert len(page["content"]) <= 6000
        chunks.append(page["content"])
        cursor = page["next_content_cursor"]
        requests += 1
        assert requests < 1000
    return json.loads("".join(chunks))


# ---------------------------------------------------------------------------------------------- initial page and detail

def test_initial_page_covers_both_collections_and_preserves_existing_fields(host):
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                         "run": "run-1", "evidence": evidence("session-1")})
    insert_run(host, "run-1", {"session-1": {"markdown": "Frozen core"}})
    insert_history(host, "candidate", [("history-1", {"state": "open"}, "2026-01-01T00:00:00Z")])
    bundle = data(host, "learnings_review_get", {"learning_id": "candidate"})
    assert bundle["complete"] is True and bundle["next_content_cursor"] is None
    assert bundle["page_collections"] == ["evidence", "history"]
    assert bundle["source_kind"] == "frozen_extraction_cores" and bundle["original_transcripts_included"] is False
    assert bundle["read_only"] is False
    assert [(source["run"], source["session"], source["status"]) for source in bundle["sources"]] == [
        ("run-1", "session-1", "available")]
    assert bundle["sources"][0]["core"]["markdown"] == "Frozen core"
    assert [event["id"] for event in bundle["history"]] == ["history-1"]
    assert (bundle["evidence_complete"], bundle["references_complete"], bundle["history_complete"]) == (True, True, True)
    assert (bundle["next_evidence_cursor"], bundle["next_history_cursor"], bundle["evidence_status"]) == (None, None, "available")
    # The ordinary factual detail keeps its own session-keyed cores projection for existing callers.
    detail = data(host, "learnings_get", {"learning_id": "candidate"})
    assert detail["cores"] == {"session-1": {"markdown": "Frozen core"}} and detail["item"]["id"] == "candidate"


def test_a_historical_delta_is_marked_read_only_with_its_preserved_fields(host):
    insert_candidate(host, "delta", {"text": "Old rule", "scope": "biz", "bucket": "delta", "tag": "rule",
                                     "evidence": evidence("session-1")})
    write(host, lambda uow, biz: uow.repository("learning_candidates", business_id=biz).update(
        "delta", {"work_state": "implemented", "agent_note": "Implemented and pushed", "work_targets_json": [{"repository": "core"}],
                  "work_outcome_json": {"kind": "changed"}}, expected_revision=1))
    bundle = data(host, "learnings_review_get", {"learning_id": "delta"})
    assert bundle["read_only"] is True
    assert bundle["item"]["state"] == "implemented" and bundle["item"]["targets"] == [{"repository": "core"}]
    assert bundle["item"]["outcome"] == {"kind": "changed"}
    assert bundle["outcome_reasoning"] == {"text": "Implemented and pushed", "source": "agent_work_explanation",
                                          "state": "implemented"}
    assert bundle["evidence_status"] == "missing"       # the run row never existed: an explicit unavailable source


# ---------------------------------------------------------------------------------------------- evidence collection

def test_more_than_two_hundred_sources_are_enumerated_through_nested_candidates(host):
    children = []
    for index in range(12):
        run = f"run-{index:02d}"
        insert_run(host, run, {f"s-{index:02d}-{position:02d}": {"markdown": f"{index}:{position}"} for position in range(20)})
        references = [evidence(f"s-{index:02d}-{position:02d}", run=run) for position in range(20)]
        child = {"id": f"child-{index:02d}", "text": f"Child {index}", "scope": "biz", "bucket": "context", "tag": "fact",
                 "run": run, "evidence": references[0], "evidence_more": references[1:]}
        children.append({"kind": "duplicate", "reason": "same fact", "automatic": True, "earlier": child})
    insert_run(host, "root-run", {"root-session": {"markdown": "root"}})
    insert_candidate(host, "root", {"text": "Root", "scope": "biz", "bucket": "context", "tag": "fact",
                                    "run": "root-run", "evidence": evidence("root-session"), "reconciliations": children})
    first = data(host, "learnings_review_get", {"learning_id": "root"})
    assert len(first["sources"]) == 20 and first["next_evidence_cursor"] and not first["evidence_complete"]
    assert first["page_collections"] == ["evidence", "history"]       # the initial request selects both
    assert first["history"] == [] and first["history_complete"] is True
    pages, sources = collect(host, "root", collection="evidence", cursor=first["next_evidence_cursor"])
    everything = first["sources"] + sources
    assert all(len(page["sources"]) <= 20 for page in pages)
    identities = [(source["run"], source["session"]) for source in everything]
    expected = {("root-run", "root-session")} | {(f"run-{i:02d}", f"s-{i:02d}-{p:02d}") for i in range(12) for p in range(20)}
    assert set(identities) == expected and len(identities) == len(set(identities)) == 241
    assert pages[-1]["evidence_complete"] is True and pages[-1]["references_complete"] is True
    assert pages[-1]["evidence_status"] == "available" and pages[-1]["next_evidence_cursor"] is None
    assert all(source["status"] == "available" and source["core"] for source in everything)


def test_more_than_one_hundred_nested_candidates_resume_even_when_pages_are_empty(host):
    children = [{"kind": "duplicate", "reason": "same", "automatic": True, "earlier": {"id": f"empty-{index:03d}"}}
                for index in range(130)]
    insert_run(host, "only-run", {"only": {"markdown": "one"}})
    insert_candidate(host, "root", {"text": "Root", "scope": "biz", "bucket": "context", "tag": "fact",
                                    "evidence": evidence("only", run="only-run"), "reconciliations": children})
    first = data(host, "learnings_review_get", {"learning_id": "root"})
    assert [source["session"] for source in first["sources"]] == ["only"]
    assert first["next_evidence_cursor"] and not first["evidence_complete"]
    pages, sources = collect(host, "root", collection="evidence", cursor=first["next_evidence_cursor"])
    assert sources == []                                   # every nested candidate carries no frozen reference
    assert any(page["sources"] == [] and page["next_evidence_cursor"] for page in pages)   # bounded empty progress
    assert pages[-1]["evidence_complete"] is True and pages[-1]["references_complete"] is True
    assert len(pages) >= 2


def test_two_runs_of_one_session_stay_distinct_frozen_sources(host):
    insert_run(host, "run-a", {"session": {"markdown": "From the first run"}})
    insert_run(host, "run-b", {"session": {"markdown": "From the second run"}})
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                         "run": "run-a", "evidence": evidence("session"),
                                         "evidence_more": [evidence("session", run="run-b")]})
    bundle = data(host, "learnings_review_get", {"learning_id": "candidate"})
    sources = {(source["run"], source["session"]): source for source in bundle["sources"]}
    assert set(sources) == {("run-a", "session"), ("run-b", "session")}
    assert sources["run-a", "session"]["core"]["markdown"] == "From the first run"
    assert sources["run-b", "session"]["core"]["markdown"] == "From the second run"


def test_missing_cores_and_unresolved_metadata_are_explicit_gaps(host):
    insert_run(host, "present", {"saved": {"markdown": "saved"}}, business=BUSINESS)
    insert_run(host, "elsewhere", {"saved": {"markdown": "other business"}}, business=SECOND)
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                         "evidence": evidence("saved", run="present"),
                                         "evidence_more": [evidence("lost", run="absent"),
                                                           evidence("saved", run="elsewhere"),
                                                           {"session": "no-run"}]})
    bundle = data(host, "learnings_review_get", {"learning_id": "candidate"})
    sources = {(source["run"], source["session"]): source for source in bundle["sources"]}
    assert set(sources) == {("present", "saved"), ("absent", "lost"), ("elsewhere", "saved")}
    assert sources["present", "saved"]["status"] == "available"
    assert sources["absent", "lost"]["status"] == "missing" and sources["absent", "lost"]["core"] is None
    # Another scope's saved core is never disclosed, and stays an explicit unavailable source here.
    assert sources["elsewhere", "saved"]["status"] == "missing" and sources["elsewhere", "saved"]["core"] is None
    # The reference without its run identity is an explicit gap: the traversal is complete but lacks metadata.
    assert bundle["evidence_complete"] is True and bundle["references_complete"] is False
    assert bundle["evidence_status"] == "partial"


# ---------------------------------------------------------------------------------------------- history collection

def test_long_history_pages_chronologically_and_returns_exact_stored_snapshots(host, monkeypatch):
    import bazarlyk.domains.learnings.review as review_module
    events = []
    for index in range(45):
        after = {"state": "open", "revision": index + 1, "text": "step " + str(index)}
        if index == 5:
            after = {**after, "text": "x" * 3000}                     # oversized history is returned complete
        if index == 6:
            after = {**after, "work_reasoning": {"text": "The owner said: keep it.", "source": "owner_direct"}}
        events.append((f"history-{index:02d}", after, f"2026-01-01T00:{index:02d}:00Z"))
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact"})
    insert_history(host, "candidate", events)
    decoded = []
    original = review_module.decode_row
    monkeypatch.setattr(review_module, "decode_row", lambda row: (decoded.append(row["id"]), original(row))[1])
    first = data(host, "learnings_review_get", {"learning_id": "candidate"})
    # A bounded page decodes only its own selected events, not the whole append-only history.
    assert len(first["history"]) == 20 and len(decoded) == 20
    assert first["history_complete"] is False and first["next_history_cursor"]
    pages, history = collect(host, "candidate", collection="history", cursor=first["next_history_cursor"])
    everything = first["history"] + history
    assert [event["id"] for event in everything] == [f"history-{index:02d}" for index in range(45)]
    assert len({event["id"] for event in everything}) == 45 and len(decoded) == 45
    assert pages[-1]["history_complete"] is True and pages[-1]["next_history_cursor"] is None
    large = next(event for event in everything if event["id"] == "history-05")["after_json"]["text"]
    assert isinstance(large, str) and large == "x" * 3000          # the actual stored value, not a preview
    reasoning = next(event for event in everything if event["id"] == "history-06")["after_json"]["work_reasoning"]
    assert reasoning == {"text": "The owner said: keep it.", "source": "owner_direct"}
    # A history-only page carries no evidence collection and never claims it complete.
    page = pages[0]
    assert page["page_collections"] == ["history"] and page["sources"] == []
    assert (page["next_evidence_cursor"], page["evidence_complete"], page["references_complete"]) == (None, False, False)


def test_a_large_history_snapshot_reassembles_through_the_byte_transport_with_exact_json(host):
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact"})
    big = {"text": "Y" * 12000, "state": "accepted", "revision": 2,
           "decision": {"state": "accepted", "note": "The owner's exact note"},
           "reconciliations": [{"kind": "duplicate", "reason": "same", "earlier": {"id": "earlier", "bucket": "context"}}],
           "work_reasoning": {"text": "Keep it.", "source": "owner_work_explanation"}}
    insert_history(host, "candidate", [("big-00", big, "2026-01-01T00:00:00Z"),
                                       *[(f"small-{index:02d}", {"text": "z" * 1500 + f" event {index}", "state": "open"},
                                          f"2026-01-01T00:{index + 1:02d}:00Z") for index in range(24)]])
    first = data(host, "learnings_review_get", {"learning_id": "candidate"})
    assert first["complete"] is False and first["next_content_cursor"] and "history" not in first
    page = reassemble(host, "candidate", first)
    assert page["history"][0]["after_json"] == big                 # byte reassembly preserves the exact stored JSON
    pages, history = collect(host, "candidate", collection="history", cursor=page["next_history_cursor"])
    everything = page["history"] + history
    assert [event["id"] for event in everything] == ["big-00", *[f"small-{index:02d}" for index in range(24)]]
    assert everything[0]["after_json"] == big
    assert pages[-1]["history_complete"] is True


def test_duplicate_stored_references_may_repeat_pages_and_merge_by_pair(host):
    insert_run(host, "repeat-run", {f"s-{index}": {"markdown": f"core {index}"} for index in range(21)})
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                         "run": "repeat-run", "evidence": evidence("s-0"),
                                         "evidence_more": [evidence(f"s-{index}") for index in range(1, 21)]
                                                          + [evidence("s-0")]})
    first = data(host, "learnings_review_get", {"learning_id": "candidate"})
    pages, sources = collect(host, "candidate", collection="evidence", cursor=first["next_evidence_cursor"])
    everything = first["sources"] + sources
    identities = [(source["run"], source["session"]) for source in everything]
    # The page position is per stored reference, so an identical stored pair can be displayed again on a later page.
    # Clients accumulate by (run, session) as the specification allows: merging must lose and duplicate no identity.
    assert len(identities) > len(set(identities))
    assert set(identities) == {("repeat-run", f"s-{index}") for index in range(21)}


def test_appended_history_invalidates_the_captured_sequence(host):
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact"})
    insert_history(host, "candidate", [(f"history-{index:02d}", {"state": "open"}, f"2026-01-01T00:{index:02d}:00Z")
                                       for index in range(25)])
    first = data(host, "learnings_review_get", {"learning_id": "candidate"})
    cursor = first["next_history_cursor"]
    insert_history(host, "candidate", [("history-appended", {"state": "open"}, "2026-01-02T00:00:00Z")])
    assert call(host, "learnings_review_get", {"learning_id": "candidate", "history_cursor": cursor}).error["code"] == "validation_error"


# ---------------------------------------------------------------------------------------------- byte continuation

def test_an_oversized_page_requires_byte_reassembly_of_that_exact_selection(host):
    insert_run(host, "run-1", {"session-1": {"markdown": "M" * 40000}})
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                         "run": "run-1", "evidence": evidence("session-1")})
    insert_history(host, "candidate", [(f"history-{index:02d}", {"state": "open"}, f"2026-01-01T00:{index:02d}:00Z")
                                       for index in range(25)])
    first = data(host, "learnings_review_get", {"learning_id": "candidate"})
    assert first["complete"] is False and "sources" not in first and first["next_content_cursor"]
    page = reassemble(host, "candidate", first)
    assert page["item"]["id"] == "candidate" and len(page["sources"][0]["core"]["markdown"]) == 40000
    assert page["next_history_cursor"] and page["next_evidence_cursor"] is None
    # A continuation with another collection selection is not this page: its bytes cannot be reassembled.
    other = call(host, "learnings_review_get", {"learning_id": "candidate", "content_cursor": first["next_content_cursor"],
                                               "history_cursor": page["next_history_cursor"]})
    assert other.error["code"] == "validation_error"
    # A byte cursor is never accepted as a collection cursor.
    wrong = call(host, "learnings_review_get", {"learning_id": "candidate", "evidence_cursor": first["next_content_cursor"]})
    assert wrong.error["code"] == "validation_error"
    # Changing the saved source between chunks invalidates the byte binding.
    write(host, lambda uow, biz: uow.connection.execute(
        "UPDATE learning_runs SET result_json=? WHERE business_id=? AND id=?",
        (json.dumps({"cores": {"session-1": {"markdown": "changed"}}}), biz, "run-1")))
    assert call(host, "learnings_review_get", {"learning_id": "candidate",
                                               "content_cursor": first["next_content_cursor"]}).error["code"] == "validation_error"


@pytest.mark.parametrize("cursor", ["garbage", "W10=", "bnVsbA==", "e30="])
def test_bad_and_obsolete_cursors_are_refused(host, cursor):
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact"})
    assert call(host, "learnings_review_get", {"learning_id": "candidate", "content_cursor": cursor}).error["code"] == "validation_error"
    assert call(host, "learnings_review_get", {"learning_id": "candidate", "evidence_cursor": cursor}).error["code"] == "validation_error"


# ---------------------------------------------------------------------------------------------- bindings and authority

def test_collection_cursors_are_bound_to_scope_candidate_version_and_kind(host):
    insert_candidate(host, "first", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                     "evidence": evidence("s-1")})
    insert_candidate(host, "second", {"text": "Another fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                      "run": "run-second",
                                      "evidence_more": [evidence(f"s-{index:02d}") for index in range(40)]})
    insert_candidate(host, "shared", {"text": "Shared id fact", "scope": "biz", "bucket": "context", "tag": "fact",
                                      "run": "run-shared",
                                      "evidence_more": [evidence(f"s-{index:02d}") for index in range(40)]},
                     business=SECOND)
    cursor = data(host, "learnings_review_get", {"learning_id": "second"})["next_evidence_cursor"]
    assert cursor
    # Cross-candidate, cross-scope and wrong-collection cursors are refused, never reinterpreted.
    assert call(host, "learnings_review_get", {"learning_id": "first", "evidence_cursor": cursor}).error["code"] == "validation_error"
    assert call(host, "learnings_review_get", {"learning_id": "second", "history_cursor": cursor}).error["code"] == "validation_error"
    # Another scope cannot even read this candidate; inside that other scope the same id is a different binding.
    other_scope = call(host, "learnings_review_get", {"learning_id": "shared"}, ctx=business_owner(host))
    assert other_scope.error["code"] == "not_found"
    assert call(host, "learnings_review_get", {"learning_id": "shared", "evidence_cursor": cursor},
                ctx=business_owner(host, SECOND)).error["code"] == "validation_error"
    own_scope = data(host, "learnings_review_get", {"learning_id": "shared"}, ctx=business_owner(host, SECOND))
    assert len(own_scope["sources"]) == 20 and own_scope["next_evidence_cursor"]
    # A changed candidate revision or effective decision invalidates the cursor.
    write(host, lambda uow, biz: uow.repository("learning_candidates", business_id=biz).update(
        "second", {"candidate_json": {"text": "Changed", "scope": "biz", "bucket": "context", "tag": "fact",
                                      "run": "run-second",
                                      "evidence_more": [evidence(f"s-{index:02d}") for index in range(40)]}},
        expected_revision=1))
    assert call(host, "learnings_review_get", {"learning_id": "second", "evidence_cursor": cursor}).error["code"] == "validation_error"
    fresh = data(host, "learnings_review_get", {"learning_id": "second"})["next_evidence_cursor"]
    insert_decision(host, "second", {"state": "open", "changes_json": {"tag": "decision"}})
    assert call(host, "learnings_review_get", {"learning_id": "second", "evidence_cursor": fresh}).error["code"] == "validation_error"


def test_global_and_business_scopes_bind_their_own_data(host):
    insert_candidate(host, "global-candidate", {"text": "Global", "scope": "global", "bucket": "context", "tag": "fact",
                                                "run": "run-global", "evidence": evidence("g-1")}, business="global")
    insert_candidate(host, "business-candidate", {"text": "Business", "scope": "biz", "bucket": "context", "tag": "fact",
                                                  "run": "run-business", "evidence": evidence("b-1")}, business=BUSINESS)
    assert call(host, "learnings_review_get", {"learning_id": "global-candidate"}).error["code"] == "not_found"
    assert call(host, "learnings_review_get", {"learning_id": "business-candidate"}, ctx=global_owner(host)).error["code"] == "not_found"
    global_bundle = data(host, "learnings_review_get", {"learning_id": "global-candidate"}, ctx=global_owner(host))
    assert global_bundle["sources"][0]["session"] == "g-1"
    business_bundle = data(host, "learnings_review_get", {"learning_id": "business-candidate"})
    assert business_bundle["sources"][0]["session"] == "b-1"


# ---------------------------------------------------------------------------------------------- agenda and input contract

def test_review_agenda_and_detail_reject_the_removed_issue_inputs(host):
    insert_candidate(host, "context", {"text": "Context", "scope": "biz", "bucket": "context", "tag": "fact"})
    insert_candidate(host, "delta", {"text": "Delta", "scope": "biz", "bucket": "delta", "tag": "rule"})
    agenda = data(host, "learnings_review_list", {})
    assert {item["id"] for item in agenda["items"]} == {"context"}
    assert {item["id"] for item in data(host, "learnings_review_list", {"bucket": "delta"})["items"]} == {"delta"}
    assert "suppressed" not in agenda and all("issue" not in item for item in agenda["items"])
    assert call(host, "learnings_review_list", {"issue_view": "all"}).error["code"] == "validation_error"
    assert call(host, "learnings_review_get", {"learning_id": "context", "issue_id": new_id()}).error["code"] == "validation_error"
    assert call(host, "learnings_review_get", {"learning_id": "context", "package_revision": 1}).error["code"] == "validation_error"
    assert call(host, "learnings_review_get", {}).error["code"] == "validation_error"


def test_reasoning_projection_keeps_owner_and_agent_attribution_separate(host):
    insert_candidate(host, "candidate", {"text": "A fact", "scope": "biz", "bucket": "context", "tag": "fact"})
    insert_decision(host, "candidate", {"state": "rejected", "feedback": "Not now", "changes_json": {}})
    insert_history(host, "candidate", [
        ("history-1", {"decision": {"note": "Not now", "reasoning_source": "owner_direct"}}, "2026-01-01T00:00:00Z"),
        ("history-2", {"decision": {"agent_rationale": "Compared the evidence"}}, "2026-01-02T00:00:00Z")])
    bundle = data(host, "learnings_review_get", {"learning_id": "candidate"})
    assert bundle["reasoning"]["text"] == "Not now" and bundle["reasoning"]["source"] == "owner_direct"
    assert bundle["agent_rationale"]["text"] == "Compared the evidence" and bundle["agent_rationale"]["source"] == "agent_rationale"
    # History events keep their own identity and are never rewritten by the projection.
    assert [event["id"] for event in bundle["history"]] == ["history-1", "history-2"]