import pytest

from qorqut_operations.contracts import DomainError, canonical_hash, new_id
from foundation_support import BUSINESS, HUMAN, database_at


def test_installation_and_job_frozen_binding_guards(tmp_path):
    db = database_at(tmp_path)
    with db.transaction() as uow:
        installation = uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": HUMAN})
        job = uow.repository("jobs", business_id=BUSINESS).insert({"id": new_id(), "target_namespace": "business:" + BUSINESS,
            "kind": "context.file", "principal_id": HUMAN, "arguments_json": {"upload_id": new_id()},
            "definition_ref_json": {"id": "context.file", "version": 1, "digest": "d"}, "dedupe_key": "source", "state": "queued"})
    assert installation["revision"] == 1
    with pytest.raises(DomainError):
        with db.transaction() as uow:
            uow.repository("jobs", business_id=BUSINESS).update(job["id"], {"arguments_json": {"upload_id": "different"}}, expected_revision=1)
    outputs = [{"operation": "context_filing_apply", "arguments": {"upload_id": "source", "notes": []}}]
    result = {"outputs": outputs, "outputs_hash": canonical_hash(outputs)}
    with db.transaction() as uow:
        current = uow.repository("jobs", business_id=BUSINESS).update(job["id"], {"result_json": result}, expected_revision=1)
    with pytest.raises(DomainError):
        with db.transaction() as uow:
            uow.repository("jobs", business_id=BUSINESS).update(job["id"], {"result_json": {"outputs": [], "outputs_hash": canonical_hash([])}}, expected_revision=current["revision"])
    with db.transaction() as uow:
        updated = uow.repository("jobs", business_id=BUSINESS).update(job["id"], {"result_json": {**result, "applied": ["one-output"]}}, expected_revision=current["revision"])
        assert updated["result_json"]["outputs"] == outputs
    checkpoints = {**updated["result_json"], "dispatch_intent": {"client_ref": "request", "specification": {"post": True}}, "external_receipt": {"job_ref": "external-1"}}
    with db.transaction() as uow:
        saved = uow.repository("jobs", business_id=BUSINESS).update(job["id"], {"result_json": checkpoints}, expected_revision=updated["revision"])
    for key in ("dispatch_intent", "external_receipt"):
        with pytest.raises(DomainError):
            with db.transaction() as uow:
                erased = {name: value for name, value in checkpoints.items() if name != key}
                uow.repository("jobs", business_id=BUSINESS).update(job["id"], {"result_json": erased}, expected_revision=saved["revision"])
