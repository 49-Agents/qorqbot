"""New contracts: small completion receipts and selected-card summaries."""
from copy import deepcopy

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.transport.mcp_presentation import (
    SIGNOFF_COMPLETION_WRITES, finalize, present, serialize, split_detail,
)
from mcp_efficiency_support import isolated_service, seed_reads
from test_service_transports import ORIGIN


def test_signoff_decision_receipt_distinguishes_queued_from_executed():
    canonical = {"status": "ok", "data": {"signoff": {
        "id": "74c1ce87-6a42-4839-908a-a313b08e3b8d", "proposal_revision": 1,
        "status_revision": 2, "state": "approved", "decision_state": "approved",
        "execution_state": "queued", "payload_hash": "reviewed-proposal-hash",
        "platform": {"submission": {"body": "Reviewed outbound text"}},
        "preview": {"title": "Reviewed proposal"}, "error": None, "apply_result": None,
        "decision": {"kind": "approve", "at": "2026-09-23T00:00:00Z"},
    }}}
    original = deepcopy(canonical)
    for operation in SIGNOFF_COMPLETION_WRITES:
        receipt = finalize(present(operation, canonical))
        result = receipt["data"]["signoff"]
        assert result["decision_state"] == "approved"
        assert result["execution_state"] == "queued"
        assert result["status_revision"] == 2
        assert "platform" not in result and "preview" not in result
        assert len(serialize(receipt)) <= 300
        assert present(operation, canonical, "full") is canonical
    assert canonical == original
    evidence = present("signoffs_get", canonical)["data"]["signoff"]
    assert evidence["platform"] == canonical["data"]["signoff"]["platform"]
    assert evidence["payload_hash"] == "reviewed-proposal-hash"


def test_signoff_receipt_keeps_execution_failures_and_followup_references():
    failure = {"code": "delivery_uncertain", "retryable": False,
               "details": {"confirmed": False, "sent": 0}}
    outcome = {"delivery_id": "delivery", "recovery": {"retry_safe": False}, "effects": []}
    canonical = {"status": "ok", "warnings": ["Reconcile before retrying"],
        "context_refresh_required": True, "configuration_revision": "new",
        "data": {"signoff": {"id": "s", "decision_state": "approved",
            "execution_state": "failed", "error": failure, "apply_result": outcome,
            "decision": {"message_id": "feedback-note", "feedback": "Already supplied"}}}}
    value = finalize(present("signoffs_email_approve", canonical))
    receipt = value["data"]["signoff"]
    assert receipt["error"] == failure and receipt["apply_result"] == outcome
    assert receipt["decision_message_id"] == "feedback-note"
    assert value["warnings"] == canonical["warnings"]
    assert value["configuration_revision"] == "new"
    error = {"status": "error", "error": failure, "request_id": "trace"}
    gated = {"status": "signoff_required", "signoff": {"id": "s", "preview": "evidence"}}
    assert present("signoffs_reject", error) is error
    assert present("signoffs_reject", gated) is gated


def test_selected_focus_summary_preserves_redaction_and_conversation_boundary():
    args, detail = split_detail("focus_get", {"item_id": "card", "response_detail": "summary"})
    canonical = {"status": "ok", "data": {"item": {
        "id": "card", "revision": 4, "unavailable": True, "archived": False}}}
    item = present("focus_get", canonical, detail, arguments=args)["data"]["item"]
    assert item == {"id": "card", "revision": 4, "unavailable": True}
    with pytest.raises(DomainError, match="does not include conversations"):
        split_detail("focus_get", {**args, "response_detail": "summary", "conversation": True})
    with pytest.raises(DomainError, match="does not include conversations"):
        present("focus_get", canonical, "summary", arguments={**args, "conversation": True})


def test_selected_focus_summary_through_mcp_keeps_revision_and_full_read(tmp_path):
    with isolated_service(tmp_path) as service:
        records = seed_reads(service)
        with TestClient(service.web) as client:
            headers = service.initialize(client, service.ordu)
            args = {"scope": "global", "item_id": records["cards"][-1]["id"]}
            ordinary = service.tool(client, headers, "focus_get", args)["decoded"]
            summary = service.tool(client, headers, "focus_get", {
                **args, "response_detail": "summary"})["decoded"]
            assert summary["status"] == ordinary["status"] == "ok"
            card = summary["data"]["item"]
            assert card["revision"] == ordinary["data"]["item"]["revision"]
            assert card["id"] == args["item_id"]
            assert card["reference"] == {key: ordinary["data"]["item"]["reference"][key]
                for key in ("key", "title", "target")}
            assert "items" not in summary["data"] and "thread" not in card
            assert len(serialize(summary)) < len(serialize(ordinary))
            thread = service.tool(client, headers, "focus_get", {
                **args, "conversation": True})["decoded"]
            assert thread["data"]["item"]["thread"]


def test_signoff_completion_mcp_full_replay_preserves_canonical_evidence(tmp_path):
    with isolated_service(tmp_path) as service:
        with TestClient(service.web) as client:
            permitted = client.post("/api/biz/widget/operations/permissions_set", headers=ORIGIN, json={
                "agent_id": service.agent.id, "overrides": {"signoffs_input_cancel": "allow"},
                "expected_revision": 1, "idempotency_key": new_id()})
            assert permitted.status_code == 200, permitted.text
            headers = service.initialize(client, service.agent)
            created = service.tool(client, headers, "signoffs_input_create", {
                "request_key": "receipt-contract", "idempotency_key": new_id(), "form": {
                    "title": "Choose a time", "explanation": "Which review time works?",
                    "kind": "question", "selection_mode": "none"}})["decoded"]
            assert created["status"] == "ok", created
            args = {"signoff_id": created["data"]["signoff"]["id"], "idempotency_key": new_id()}
            receipt = service.tool(client, headers, "signoffs_input_cancel", args)["decoded"]
            assert receipt["status"] == "ok", receipt
            assert len(serialize(receipt)) <= 300
            assert "input" not in receipt["data"]["signoff"]
            full = service.tool(client, headers, "signoffs_input_cancel", {
                **args, "response_detail": "full"})["decoded"]
            assert full["status"] == "ok", full
            canonical = full["data"]["signoff"]
            assert canonical["id"] == args["signoff_id"]
            assert canonical["status_revision"] == receipt["data"]["signoff"]["status_revision"]
            assert canonical["input"]["explanation"] == "Which review time works?"
            with service.fixture.db.read() as uow:
                row = uow.connection.execute("SELECT status_revision FROM signoffs WHERE id=?",
                    (args["signoff_id"],)).fetchone()
                assert row[0] == canonical["status_revision"]
