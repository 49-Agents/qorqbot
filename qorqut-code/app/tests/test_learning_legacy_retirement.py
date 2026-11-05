"""Focused proof of the retired procedural learning route after its engine, issue and work machinery were removed.

The factual Context route stays operational: reads, review decisions on Context items and extraction jobs still work.
New procedural delta mutations, engine imports and relations that would mutate a retained delta are refused atomically.
The ten retired operations and the engine program are not registered at all, and no retained reader queries a removed
table. Retained legacy delta rows remain readable, and the bounded pending inventory is read-only.
"""
from types import SimpleNamespace
import json

import pytest

from bazarlyk.domains.learnings import legacy
from qorqut_operations.contracts import (DomainError, InvocationContext, InvocationOptions, Principal, new_id)
from host_foundation_support import owner_global_context
from native_chat_support import Native

DELTA_ID = "5b1d0e0e-0000-4000-8000-0000000000d1"
CONTEXT_ID = "5b1d0e0e-0000-4000-8000-0000000000c1"

RETIRED_OPERATIONS = ("learnings_engine_import", "learnings_engine_enrollment_set", "learnings_issue_action",
                      "learnings_work_list", "learnings_work_get", "learnings_work_start", "learnings_work_defer",
                      "learnings_work_done", "learnings_work_drop", "learnings_work_note_set")
FACTUAL_OPERATIONS = ("learnings_extract", "learnings_record_batch", "learnings_list", "learnings_get",
                      "learnings_review_list", "learnings_review_get", "learnings_review", "learnings_edit",
                      "learnings_decide", "learnings_keep_both", "learnings_intake_status", "learnings_legacy_pending")
REMOVED_TABLES = ("learning_issues", "learning_issue_candidates", "learning_issue_actions", "learning_issue_links",
                  "learning_engine_packages", "learning_engine_enrollments", "learning_engine_state")


def candidate(bucket, text, scope="biz"):
    return {"bucket": bucket, "tag": "fact" if bucket == "context" else "rule", "scope": scope,
            "text": text, "created": "2026-01-01T00:00:00Z"}


def seed(native):
    with native.runtime.database.transaction() as uow:
        for identity, bucket, text in ((DELTA_ID, "delta", "legacy retained rule"),
                                       (CONTEXT_ID, "context", "real fact")):
            uow.repository("learning_candidates", business_id=None).insert({
                "id": identity, "candidate_json": candidate(bucket, text, "global" if bucket == "context" else "biz"),
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})


def invoke(native, name, arguments, ctx=None, revision=None):
    options = InvocationOptions(idempotency_key=new_id())
    if revision is not None:
        options = InvocationOptions(idempotency_key=new_id(), expected_revision=revision)
    return native.runtime.application.invoke(ctx or owner_global_context(native.instance), name, arguments, options)


def prepare(native, name, arguments):
    """Call one operation's prepare with a worker principal, so policy cannot mask the domain guard."""
    ctx = InvocationContext(Principal(new_id(), "system"), business_id=None, source="worker")
    descriptor = native.runtime.registry.get(name)
    with native.runtime.database.transaction() as uow:
        return descriptor.prepare(ctx, arguments, SimpleNamespace(expected_revision=1, idempotency_key=new_id()), uow)


def test_removed_operations_and_the_engine_program_are_unregistered_and_factual_ones_remain(tmp_path):
    # The synthetic fixture uses the checkout's role sources; the parallel role update is not part of this slice.
    from qorqut_operations.background.programs import ProgramRegistry
    from bazarlyk.background.integration import register_programs
    native = Native(tmp_path, "claude")
    for name in RETIRED_OPERATIONS:
        assert not native.runtime.registry.known(name), name
    for name in FACTUAL_OPERATIONS:
        assert native.runtime.registry.known(name), name
        assert native.runtime.registry.get(name).name == name
    # A call to a removed name is the ordinary unknown-operation response and mutates nothing.
    seed(native)
    for name, arguments in (("learnings_work_start", {"learning_id": DELTA_ID, "expected_revision": 1}),
                            ("learnings_issue_action", {"issue_id": "issue", "action": "accept"}),
                            ("learnings_engine_import", {"engine_id": "e"}),
                            ("learnings_engine_enrollment_set", {"agent_id": new_id()})):
        refused = invoke(native, name, arguments)
        assert refused.status == "error" and refused.error["code"] == "operation_unavailable", (name, refused.error)
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_candidates").fetchone()[0] == 2
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0] == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0] == 0
    # The nine retained programs include the two factual learning programs; the old engine program is absent.
    programs = ProgramRegistry()
    register_programs(programs)
    assert {program.id for program in programs} >= {"learnings.extract", "learnings.extract_global"}
    assert programs.get("learnings.engine_extract") is None
    with pytest.raises(DomainError) as unavailable:
        programs.require("learnings.engine_extract")
    assert unavailable.value.code == "operation_unavailable"


def test_legacy_inventory_is_bounded_read_only_history_and_delta_writes_are_refused(tmp_path):
    native = Native(tmp_path, "claude")
    seed(native)
    listed = invoke(native, "learnings_legacy_pending", {})
    assert listed.status == "ok", listed.error
    inventory = listed.data
    assert inventory["read_only"] is True
    assert [item["learning_id"] for item in inventory["items"]] == [DELTA_ID]
    assert inventory["items"][0]["scope"] == "biz" and inventory["items"][0]["decision"] is None
    assert inventory["items"][0]["text_truncated"] is False and "reconciled_into" in inventory["items"][0]
    assert "historical procedural records" in inventory["statement"]
    # Retained delta reads stay valid; the decision/review/edit work and keep-both writes do not.
    assert invoke(native, "learnings_get", {"learning_id": DELTA_ID}).status == "ok"
    for name, arguments in (
        ("learnings_review", {"learning_id": DELTA_ID, "state": "rejected"}),
        ("learnings_edit", {"learning_id": DELTA_ID, "changes": {"text": "changed"}}),
    ):
        refused = invoke(native, name, arguments, revision=1)
        assert refused.status == "error" and refused.error["code"] == "forbidden", (name, refused.error)
    for name, arguments in (
        ("learnings_keep_both", {"learning_id": DELTA_ID, "expected_revision": 1}),
        ("learnings_decide", {"learning_id": DELTA_ID, "state": "accepted", "owner_instruction": "change it",
                              "expected_revision": 1}),
    ):
        with pytest.raises(DomainError) as refused:
            prepare(native, name, arguments)
        assert refused.value.code == "forbidden", name
    # The factual Context path still works: reads, an owner review decision and the extraction job.
    assert invoke(native, "learnings_get", {"learning_id": CONTEXT_ID}).status == "ok"
    reviewed = invoke(native, "learnings_review", {"learning_id": CONTEXT_ID, "state": "rejected"}, revision=1)
    assert reviewed.status == "ok", reviewed.error
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT state FROM learning_decisions WHERE candidate_id=?",
                                      (CONTEXT_ID,)).fetchone()[0] == "rejected"


def test_worker_output_refuses_a_procedural_or_mixed_batch_atomically(tmp_path):
    native = Native(tmp_path, "claude")
    seed(native)
    core = {"markdown": "## T1  OWNER\n\nwe keep the cloud tier paid\n", "header": {"biz": None},
            "evidence_blocks": {"T1": {"role": "owner", "text": "we keep the cloud tier paid"}}}
    item = {"id": new_id(), "bucket": "delta", "tag": "rule", "scope": "global", "scope_reason": "global rule",
            "text": "never tag an unreviewed build", "said_by": "owner", "from_correction": False, "confidence": 0.9,
            "evidence": {"turns": ["T1"], "owner_quote": "we keep the cloud tier paid", "agent_quote": None}}
    for items in ([item], [{**item, "id": new_id(), "bucket": "context", "tag": "fact", "text": "a fact"}, item]):
        with pytest.raises(DomainError) as refused:
            prepare(native, "learnings_record_batch", {"items": items, "run": {"id": new_id(), "cores": {"s": core}}})
        assert refused.value.code == "forbidden"
    # A Context-only batch is not what this check exercises; the atomic refusal wrote nothing at all.
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_candidates").fetchone()[0] == 2
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_runs").fetchone()[0] == 0
    # The producer keeps only the Context subset before composing any output.
    assert legacy.context_only_items([item, {**item, "bucket": "context"}]) == [{**item, "bucket": "context"}]


def test_effective_delta_conversion_is_refused_and_history_stays_fenced(tmp_path):
    native = Native(tmp_path, "claude")
    seed(native)
    # The demonstrated conversion: a Context candidate's bucket cannot be edited into a delta.
    refused = invoke(native, "learnings_edit", {"learning_id": CONTEXT_ID,
                                                "changes": {"bucket": "delta"}}, revision=1)
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.error
    with native.runtime.database.read() as uow:
        assert json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (CONTEXT_ID,)).fetchone()[0]) == candidate(
                "context", "real fact", "global")
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0] == 0
    # A historical row whose original bucket was Context but whose retained decision now says delta is effective delta.
    with native.runtime.database.transaction() as uow:
        uow.repository("learning_candidates", business_id=None).insert({
            "id": "5b1d0e0e-0000-4000-8000-0000000000c2",
            "candidate_json": {**candidate("context", "converted history", "global"), "created": "2026-01-02T00:00:00Z"},
            "created_at": "2026-01-02T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"})
        uow.repository("learning_decisions", business_id=None).insert({
            "candidate_id": "5b1d0e0e-0000-4000-8000-0000000000c2", "state": "accepted",
            "changes_json": {"bucket": "delta", "tag": "rule"}, "decided_by": owner_global_context(native.instance).principal_id})
    for name, arguments in (
        ("learnings_review", {"learning_id": "5b1d0e0e-0000-4000-8000-0000000000c2", "state": "rejected"}),
        ("learnings_edit", {"learning_id": "5b1d0e0e-0000-4000-8000-0000000000c2", "changes": {"text": "rewritten"}}),
    ):
        result = invoke(native, name, arguments, revision=1)
        assert result.status == "error" and result.error["code"] == "forbidden", (name, result.error)
    inventory = invoke(native, "learnings_legacy_pending", {})
    assert "5b1d0e0e-0000-4000-8000-0000000000c2" in [item["learning_id"] for item in inventory.data["items"]]
    # Context remains reviewable and its real filing path still works: attach the stock background programs so the
    # accepted Context item queues its ordinary filing job (no model or provider is called).
    from bazarlyk.background.integration import attach_background
    attach_background(native.runtime, native.runtime.programs, model_client_factory=lambda _business: None)
    with native.runtime.database.transaction() as uow:
        native.runtime.authority.register_workers(uow, [])
    accepted = invoke(native, "learnings_review", {"learning_id": CONTEXT_ID, "state": "accepted"}, revision=1)
    assert accepted.status == "ok", accepted.error
    with native.runtime.database.read() as uow:
        filing = uow.connection.execute("SELECT filing_json FROM learning_candidates WHERE id=?",
                                        (CONTEXT_ID,)).fetchone()[0]
    assert filing and filing != "{}"


def test_context_is_not_broadened_into_procedural_work(tmp_path):
    native = Native(tmp_path, "claude")
    seed(native)
    with native.runtime.database.transaction() as uow:
        uow.repository("learning_decisions", business_id=None).insert({
            "candidate_id": CONTEXT_ID, "state": "accepted", "decided_by": owner_global_context(native.instance).principal_id})
    for name in ("learnings_work_start", "learnings_work_done"):
        assert not native.runtime.registry.known(name)
        with pytest.raises(DomainError) as unavailable:
            native.runtime.registry.get(name)
        assert unavailable.value.code == "operation_unavailable"
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT work_state FROM learning_candidates WHERE id=?",
                                      (CONTEXT_ID,)).fetchone()[0] is None


def test_inventory_is_bounded_pages_effective_state_and_decodes_filing(tmp_path):
    native = Native(tmp_path, "claude")
    seed(native)
    with native.runtime.database.transaction() as uow:
        for index in range(4):
            uow.repository("learning_candidates", business_id=None).insert({
                "id": f"5b1d0e0e-0000-4000-8000-0000000000e{index}",
                "candidate_json": {**candidate("delta", f"legacy {index}"), "created": f"2026-01-0{index+1}T00:00:00Z"},
                "created_at": f"2026-01-0{index+1}T00:00:00Z", "updated_at": f"2026-01-0{index+1}T00:00:00Z",
                "filing_json": {"upload_id": "old"} if index == 1 else {}})
    first = invoke(native, "learnings_legacy_pending", {"limit": 2})
    assert first.status == "ok", first.error
    assert first.data["count"] == 5 and len(first.data["items"]) == 2 and first.data["next_cursor"]
    second = invoke(native, "learnings_legacy_pending", {"limit": 2, "cursor": first.data["next_cursor"]})
    assert second.data["count"] == 5 and len(second.data["items"]) == 2
    third = invoke(native, "learnings_legacy_pending", {"limit": 2, "cursor": second.data["next_cursor"]})
    identifiers = [item["learning_id"] for item in first.data["items"] + second.data["items"] + third.data["items"]]
    assert len(identifiers) == len(set(identifiers)) == 5
    filed = next(item for item in first.data["items"] + second.data["items"] + third.data["items"]
                 if item["learning_id"] == "5b1d0e0e-0000-4000-8000-0000000000e1")
    assert filed["filed"] is True


def test_indirect_relation_to_an_effective_historical_delta_is_refused(tmp_path):
    native = Native(tmp_path, "claude")
    seed(native)
    with native.runtime.database.transaction() as uow:
        uow.repository("learning_decisions", business_id=None).insert({
            "candidate_id": CONTEXT_ID, "state": "accepted",
            "decided_by": owner_global_context(native.instance).principal_id,
            "changes_json": {"bucket": "delta"}})
    core = {"markdown": "## T1  OWNER\n\nwe keep the cloud tier paid\n", "header": {"biz": None},
            "evidence_blocks": {"T1": {"role": "owner", "text": "we keep the cloud tier paid"}}}
    item = {"id": new_id(), "bucket": "context", "tag": "fact", "scope": "global", "scope_reason": "global fact",
            "text": "a fact", "said_by": "owner", "from_correction": False, "confidence": 0.9,
            "evidence": {"turns": ["T1"], "owner_quote": "we keep the cloud tier paid", "agent_quote": None},
            "reconciliations": [{"automatic": True, "earlier": {"id": DELTA_ID}, "reason": "same rule", "kind": "duplicate",
                                 "assessment": {}, "evidence": {"turns": ["T1"]}}]}
    with pytest.raises(DomainError) as refused:
        prepare(native, "learnings_record_batch", {"items": [item], "evidence": [
            {"of": CONTEXT_ID, "kind": "reconciled", "into": item["id"], "revision": 1,
             "reconciliations": item["reconciliations"]}],
            "run": {"id": new_id(), "cores": {"s": core}}})
    assert refused.value.code == "forbidden"
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_runs").fetchone()[0] == 0
        retained = json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (CONTEXT_ID,)).fetchone()[0])
    assert retained["bucket"] == "context"


def test_owned_sources_query_no_removed_engine_table(tmp_path):
    """The retained domain and background sources never query one of the seven removed tables (static source check)."""
    from pathlib import Path
    import re
    import bazarlyk.domains.learnings as learnings
    root = Path(learnings.__file__).parent.parent.parent
    owned = [root / "domains/learnings", root / "background/learnings.py", root / "background/integration.py"]
    texts = {}
    for entry in owned:
        paths = sorted(entry.rglob("*.py")) if entry.is_dir() else [entry]
        for path in paths:
            texts[path] = path.read_text()
    for table in REMOVED_TABLES:
        patterns = (rf"\b(?:FROM|INTO|JOIN|UPDATE)\s+['\"]?{table}\b",
                    rf"repository\(\s*['\"]{table}['\"]", rf"rows\([^)]*['\"]{table}['\"]")
        offenders = [str(path) for path, text in texts.items() if any(re.search(pattern, text) for pattern in patterns)]
        assert offenders == [], (table, offenders)
    assert not (root / "background/learning_engine.py").exists()