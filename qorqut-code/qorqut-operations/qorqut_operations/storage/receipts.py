"""Durable idempotency and recoverable-application bookkeeping."""

from __future__ import annotations

from qorqut_operations.contracts import ChangeSet, DomainError, OperationResult, canonical_hash, new_id, plain


class IdempotencyReceipts:
    def __init__(self, uow):
        self.uow = uow
        self.repo = uow.repository("idempotency_receipts")

    @staticmethod
    def key(principal_id, namespace, operation, key):
        return {"principal_id": principal_id, "target_namespace": namespace,
                "operation_name": operation, "idempotency_key": key}

    def lookup(self, principal_id, namespace, operation, key, input_hash):
        row = self.repo.get(self.key(principal_id, namespace, operation, key))
        if row is None:
            return None
        if row["input_hash"] != input_hash:
            raise DomainError("idempotency_conflict", "Idempotency key was already used for different input")
        return OperationResult.from_dict(row["result_json"])

    def save(self, principal_id, namespace, operation, key, input_hash, result):
        old = self.lookup(principal_id, namespace, operation, key, input_hash)
        if old is not None:
            return old
        self.repo.insert({**self.key(principal_id, namespace, operation, key), "input_hash": input_hash,
                          "result_json": result.to_dict()})
        return result


class ApplyReceipts:
    """Resource availability and file history are the retained domain's: the assembly's lifecycle
    callbacks (by resource type) and receipt history are called at exactly the points the v1 receipts
    updated document availability and captured shared history."""

    def __init__(self, uow):
        self.uow = uow
        self.repo = uow.repository("apply_receipts")
        self.assembly = uow.database.assembly

    def _lifecycle(self, resource):
        return self.assembly.lifecycles.get(resource.resource_type)

    def get(self, receipt_id):
        return self.repo.get(receipt_id, required=True)

    def create(self, plan: ChangeSet, *, receipt_id=None, signoff_id=None):
        receipt_id = receipt_id or new_id()
        old = self.repo.get(receipt_id)
        if old is not None:
            if old["payload_hash"] != plan.payload_hash or old["signoff_id"] != signoff_id:
                raise DomainError("idempotency_conflict", "Application receipt is bound to another prepared change")
            return old
        return self.repo.insert({"id": receipt_id, "principal_id": plan.principal_id,
                                 "target_namespace": plan.target_namespace, "operation_name": plan.operation_name,
                                 "payload_hash": plan.payload_hash, "change_set": plan.canonical,
                                 "signoff_id": signoff_id, "state": "prepared", "progress_json": {}})

    @staticmethod
    def resource_key(plan, resource):
        return {"target_namespace": ("business:" + resource.business_id) if resource.business_id
                else resource.namespace or plan.target_namespace,
                "resource_type": resource.resource_type, "resource_id": resource.resource_id}

    @staticmethod
    def progress_key(resource):
        return canonical_hash(resource)

    def claim_resources(self, receipt_id):
        self.uow.require_write()
        row = self.get(receipt_id)
        plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
        if row["state"] == "applied":
            return row
        claims = self.uow.repository("resource_claims")
        for change in plan.changes:
            key = self.resource_key(plan, change.resource)
            current = claims.get(key)
            if current and current["apply_id"] != receipt_id:
                raise DomainError("busy", "Resource has an unfinished application", retryable=True)
            if not current:
                claims.insert({**key, "apply_id": receipt_id})
            lifecycle = self._lifecycle(change.resource)
            if lifecycle is not None:
                lifecycle.applying(self.uow, change.resource)
        if row["state"] != "applying":
            row = self.repo.update(receipt_id, {"state": "applying", "error_json": None}, expected_revision=row["revision"])
        return row

    def record_progress(self, receipt_id, key, value):
        row = self.get(receipt_id)
        progress = {**row["progress_json"], key: plain(value)}
        return self.repo.update(receipt_id, {"progress_json": progress}, expected_revision=row["revision"])

    def fail(self, receipt_id, error: DomainError):
        row = self.get(receipt_id)
        state = "conflicted" if error.code == "revision_conflict" else "failed"
        if state == "conflicted":
            plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
            for change in plan.changes:
                lifecycle = self._lifecycle(change.resource)
                if lifecycle is not None:
                    lifecycle.conflicted(self.uow, change.resource)
        return self.repo.update(receipt_id, {"state": state, "error_json": error.to_dict()},
                                expected_revision=row["revision"])

    def finish(self, receipt_id, result):
        row = self.get(receipt_id)
        if row["state"] == "applied":
            return row
        plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
        for change in plan.changes:
            if change.kind == "file":
                progress = row["progress_json"].get(self.progress_key(change.resource), {})
                if not progress.get("verified") or progress.get("after_hash") != change.values["after_hash"]:
                    raise DomainError("apply_incomplete", "Cannot finalize unverified file effects")
        # Verified final bytes become shared history in this same finalising transaction.
        self.assembly.receipt_history.capture_receipt(self.uow, row, plan)
        row = self.repo.update(receipt_id, {"state": "applied", "result_json": plain(result), "error_json": None},
                                expected_revision=row["revision"])
        for change in plan.changes:
            lifecycle = self._lifecycle(change.resource)
            if lifecycle is not None:
                lifecycle.applied(self.uow, change.resource)
        self.uow.connection.execute("DELETE FROM resource_claims WHERE apply_id = ?", (receipt_id,))
        return row

    def release_unapplied(self, receipt_id, error: DomainError, *, verified_before):
        """Trusted file executor supplies proof, never absence of progress as proof."""
        row = self.get(receipt_id)
        if row["state"] == "applied":
            raise DomainError("apply_incomplete", "Applied effects cannot be abandoned")
        plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
        expected = {self.progress_key(change.resource): change.values["before_hash"]
                    for change in plan.changes if change.kind == "file"}
        if verified_before != expected:
            raise DomainError("apply_incomplete", "Every file must be verified unchanged before releasing claims")
        self.fail(receipt_id, error)
        for change in plan.changes:
            lifecycle = self._lifecycle(change.resource)
            if lifecycle is not None:
                lifecycle.released(self.uow, change.resource)
        self.uow.connection.execute("DELETE FROM resource_claims WHERE apply_id = ?", (receipt_id,))
        return self.get(receipt_id)
