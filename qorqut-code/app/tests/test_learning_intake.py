import pytest

from qorqut_operations.contracts import DomainError, new_id
from foundation_support import BUSINESS, context
from test_background_programs import runtime, invoke


def candidate(runtime, tag="decision", bucket="context", identity="candidate"):
    with runtime.database.transaction() as uow:
        uow.repository("learning_candidates", business_id=BUSINESS).insert({"id": identity,
            "candidate_json": {"text": "Use the old launch date?\n\nCheck with Ada.", "scope": "biz", "bucket": bucket, "tag": tag}})
    return identity


@pytest.mark.parametrize("tag", ["fact", "decision", "rule", "idea", "question"])
def test_all_context_tags_route_once_with_owner_correction_and_no_tasks(runtime, tag):
    identity = candidate(runtime, tag)
    topic = invoke(runtime, "context_topics_create", {"title": "Launch", "slug": "launch"})["document"]
    result = invoke(runtime, "learnings_review", {"learning_id": identity, "state": "accepted", "feedback": "No: launch is postponed."}, 1)
    association = result["item"]["filing"]
    assert association["upload_id"] and association["job_id"]
    again = invoke(runtime, "learnings_review", {"learning_id": identity, "state": "accepted"}, 1)
    assert again["item"]["filing"] == association
    assert not runtime.application.invoke(context(), "learnings_list", {}).data["items"]
    get = runtime.application.invoke(context(), "learnings_get", {"learning_id": identity}).data
    assert get["item"]["text"] == "Use the old launch date?\n\nCheck with Ada."
    runtime.model.answers.append({"notes": [{"title": "Launch timing", "topic_id": topic["id"], "paragraph_ids": [0]}],
        "tasks": [{"title": "Check with Ada", "source_quote": "Check with Ada."}]})
    runtime.model.answers.append({"notes": [{"title": "Launch timing", "topic_id": topic["id"], "paragraph_ids": [0]}]})
    outcome = runtime.background.run_one(association["job_id"])
    assert outcome["state"] == "succeeded", outcome
    assert "Filing cannot create tasks" in runtime.model.calls[1][0][-1]["content"]
    with runtime.database.read() as uow:
        docs = uow.repository("documents", business_id=BUSINESS).list()
        notes = [row for row in docs if row["kind"] == "context_note"]
        assert len(notes) == 1
        body = runtime.domains.documents.store.view(notes[0], uow)["body"]
        assert f'tag="{tag}"' in body and "No: launch is postponed." in body
        assert "Use the old launch date?\n\nCheck with Ada." in body
        assert not uow.repository("tasks", business_id=BUSINESS).list()
        assert len([x for x in uow.repository("jobs", business_id=BUSINESS).list() if x["kind"] == "context.file"]) == 1
        assert len(uow.repository("learning_history", business_id=BUSINESS).list()) == 1
    assert runtime.application.invoke(context(), "learnings_review", {"learning_id": identity, "state": "open"},
        {"expected_revision": 2, "idempotency_key": new_id()}).status == "error"


def test_delta_decision_does_not_route_and_owner_rejection_does_not_route(runtime):
    candidate(runtime, bucket="delta", identity="delta")
    candidate(runtime, identity="rejected")
    invoke(runtime, "learnings_review", {"learning_id": "delta", "state": "accepted"}, 1)
    invoke(runtime, "learnings_review", {"learning_id": "rejected", "state": "rejected"}, 1)
    with runtime.database.read() as uow:
        assert not uow.repository("jobs", business_id=BUSINESS).list()


def test_failed_intake_is_not_hidden_or_accepted_and_same_key_can_resume(runtime, monkeypatch):
    candidate(runtime)
    original = runtime.file_applier.apply
    monkeypatch.setattr(runtime.file_applier, "apply", lambda *a, **k: (_ for _ in ()).throw(DomainError("operation_failed", "Injected failure")))
    args = {"learning_id": "candidate", "state": "accepted", "feedback": "Correction"}
    options = {"expected_revision": 1, "idempotency_key": "intake"}
    result = runtime.application.invoke(context(), "learnings_review", args, options)
    assert result.status == "error"
    item = runtime.application.invoke(context(), "learnings_get", {"learning_id": "candidate"}).data["item"]
    assert item["state"] == "open" and not item["filing"]
    monkeypatch.setattr(runtime.file_applier, "apply", original)
    result = runtime.application.invoke(context(), "learnings_review", args, options)
    assert result.status == "ok", result.to_dict()
    assert result.data["item"]["filing"]["job_id"]


def test_partial_file_failure_keeps_one_receipt_and_owner_can_resume(runtime):
    candidate(runtime)
    def crash(resource):
        raise OSError("Injected failure after replacement")
    runtime.file_applier.after_replace = crash
    args = {"learning_id": "candidate", "state": "accepted", "feedback": "Keep correction"}
    result = runtime.application.invoke(context(), "learnings_review", args,
        {"expected_revision": 1, "idempotency_key": "first-intake"})
    assert result.status == "error"
    status = runtime.application.invoke(context(), "learnings_intake_status", {"learning_id": "candidate"}).data
    assert status["pending"]["state"] == "failed"
    second = runtime.application.invoke(context(), "learnings_review", args,
        {"expected_revision": 1, "idempotency_key": "another-key"})
    assert second.error["code"] == "busy"
    assert second.error["details"]["intake_receipt_id"] == status["pending"]["receipt_id"]
    runtime.file_applier.after_replace = None
    resumed = runtime.background.learnings.resume_intake(context(), "candidate")
    assert resumed.status == "ok", resumed.to_dict()
    with runtime.database.read() as uow:
        assert len([r for r in uow.repository("apply_receipts").list() if r["operation_name"] == "learnings_review"]) == 1
        assert len(uow.repository("jobs", business_id=BUSINESS).list()) == 1
        assert not uow.repository("resource_claims").list()


def test_legacy_accepted_context_only_files_on_explicit_review(runtime):
    from foundation_support import HUMAN
    candidate(runtime)
    with runtime.database.transaction() as uow:
        uow.repository("learning_decisions", business_id=BUSINESS).insert({"candidate_id": "candidate",
            "state": "accepted", "feedback": "Historical owner correction", "changes_json": {}, "decided_by": HUMAN})
    preview = runtime.application.invoke(context(), "learnings_list", {"bucket": "context", "state": "accepted"}).data
    assert len(preview["items"]) == 1 and not preview["items"][0]["filing"]
    with runtime.database.read() as uow:
        assert not uow.repository("jobs", business_id=BUSINESS).list()
    applied = invoke(runtime, "learnings_review", {"learning_id": "candidate", "state": "accepted"}, 1)
    assert applied["item"]["decision"]["note"] == "Historical owner correction"
    assert applied["item"]["filing"]


def test_reclassifying_accepted_delta_as_context_routes_it(runtime):
    candidate(runtime, bucket="delta")
    invoke(runtime, "learnings_review", {"learning_id": "candidate", "state": "accepted", "feedback": "Owner correction"}, 1)
    changed = invoke(runtime, "learnings_edit", {"learning_id": "candidate", "changes": {"bucket": "context"}}, 2)
    assert changed["item"]["filing"]["job_id"] and changed["item"]["decision"]["note"] == "Owner correction"


def test_resume_refuses_unexpected_file_bytes(runtime):
    candidate(runtime)
    touched = []
    def crash(resource):
        touched.append(resource)
        raise OSError("Injected partial application")
    runtime.file_applier.after_replace = crash
    result = runtime.application.invoke(context(), "learnings_review", {"learning_id": "candidate", "state": "accepted"},
        {"expected_revision": 1, "idempotency_key": "conflicting-intake"})
    assert result.status == "error" and touched
    runtime.file_applier.resolve_target(touched[0]).resolve().write_text("Unrelated external edit")
    runtime.file_applier.after_replace = None
    with pytest.raises(DomainError) as error:
        runtime.background.learnings.resume_intake(context(), "candidate")
    assert error.value.code == "revision_conflict"
    with runtime.database.read() as uow:
        row = uow.repository("learning_candidates", business_id=BUSINESS).get("candidate")
        assert not row["filing_json"]
        assert not uow.repository("jobs", business_id=BUSINESS).list()
        assert uow.repository("resource_claims").list()


def test_retry_uses_original_owner_authority(runtime):
    from dataclasses import replace
    from foundation_support import HUMAN
    from qorqut_operations.contracts import Principal
    candidate(runtime)
    runtime.file_applier.after_replace = lambda resource: (_ for _ in ()).throw(OSError("Injected interruption"))
    result = runtime.application.invoke(context(), "learnings_review", {"learning_id": "candidate", "state": "accepted"},
        {"expected_revision": 1, "idempotency_key": "revoked-intake"})
    assert result.status == "error"
    second_id = new_id()
    with runtime.database.transaction() as uow:
        uow.repository("principals").insert({"id": second_id, "kind": "human", "display_name": "Second owner"})
        members = uow.repository("business_memberships", business_id=BUSINESS)
        members.insert({"principal_id": second_id, "is_owner": True})
        original = members.get(HUMAN)
        members.update(HUMAN, {"active": False}, expected_revision=original["revision"])
    second = replace(context(), principal=Principal(second_id, "human"))
    assert runtime.application.invoke(second, "learnings_intake_status", {"learning_id": "candidate"}).status == "ok"
    runtime.file_applier.after_replace = None
    with pytest.raises(DomainError) as error:
        runtime.background.learnings.resume_intake(second, "candidate")
    assert error.value.code in {"forbidden", "policy_denied"}
    with runtime.database.read() as uow:
        assert not uow.repository("learning_candidates", business_id=BUSINESS).get("candidate")["filing_json"]
