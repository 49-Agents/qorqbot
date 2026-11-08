"""Read-only proof that a saved factual plan is still resumable under the target contracts.

A pending Application receipt or Signoff is never classified from its own row. The proof reuses, call for call, what the real
runtime does before it would resume the work: the executor's ``resume`` (original actor, namespace, invocation and options
reconstructed from the saved receipt and the ordinary policy contexts) followed by ``Application.validate_prepared`` with the
executor's pre-file validation flags, or the Signoff service's own ``_validate`` for a pending (pre-approval) or approved
(real approval binding) proposal. ``validate_prepared`` composes the target authority, scope, operation version,
expected-target, receipt and approval checks and the composed learning/document validators, which receive the actual composed
Context filing ``source_bytes`` limit (never a default).

Nothing here executes, resumes, recovers or writes: the caller's snapshot (including its final ``BEGIN IMMEDIATE``
transaction) is wrapped in a read-only unit of work and SQLite's ``query_only`` is on for the duration of every proof. A
proof that fails is an incompatibility the migration blocks on; it never rebinds or rewrites a receipt.
"""

from __future__ import annotations

import sqlite3
from uuid import UUID
from contextlib import contextmanager

from qorqut_operations.contracts import ChangeSet, DomainError, InvocationOptions, canonical_hash, plain
from qorqut_operations.storage.database import UnitOfWork
from qorqut_operations.storage.executor import PreparedFileExecutor
from qorqut_operations.storage.receipts import ApplyReceipts
from qorqut_operations.storage.repositories import decode_row


def prove_input_request(target, connection, signoff_id):
    """Validate a stored question as an already-created input, without replaying its creation."""
    from qorqut_operations.signoffs.inputs import normalize_attention, validate_form
    from qorqut.bootstrap.learning_removal import _references

    if target is None or target.runtime is None:
        raise DomainError("unverified_pending_plan", "The target composition is unavailable")
    with _read_only(connection, target.runtime.database):
        row = decode_row(connection.execute("SELECT * FROM signoffs WHERE id=?", (signoff_id,)).fetchone())
        try:
            if row is None or row["category"] != "input_request" or row["state"] != "pending":
                raise ValueError("not a pending input")
            plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
            descriptor = target.runtime.application.registry.get("signoffs_input_create")
            args = descriptor.validate(plain(plan.arguments))
            form = validate_form(args["form"])
            business = row["review_business_id"]
            scope = {"kind": "business", "business_id": business} if business else {"kind": "global"}
            namespace = "business:" + business if business else "global"
            UUID(signoff_id)
            if (row["operation_name"] != descriptor.name or row["operation_version"] != descriptor.version
                    or plan.operation_name != descriptor.name or plan.operation_version != descriptor.version
                    or plan.principal_id != row["proposer_principal_id"]
                    or plan.proposer_agent_id != row["proposer_agent_id"]
                    or plan.initiated_by_principal_id != row["initiated_by_principal_id"]
                    or plan.review_business_id != business or row["review_scope_kind"] != scope["kind"]
                    or plan.target_namespace != namespace or plain(plan.affected_scope) != scope
                    or plain(plan.policy_binding) != row["policy_binding_json"]
                    or row["affected_scope_json"] != scope or row["request_key"] != args["request_key"]
                    or (args.get("scope") == "global" and business is not None)
                    or (args.get("scope") == "business" and business is None)
                    or row["preview_json"] != {"title": form["title"], "explanation": form["explanation"], "input": form}
                    or row["expected_targets_json"] != [] or row["superseded_by_id"] is not None
                    or _references(plan.to_dict()) or len(plan.changes) != 1 or len(plan.expected_targets) != 1):
                raise ValueError("question binding mismatch")
            change, expected = plan.changes[0], plan.expected_targets[0]
            resource = change.resource
            values = {"form": form, "request_key": args["request_key"],
                      "attention": normalize_attention(args.get("attention", {}))}
            if (change.kind != "record" or change.action != "input_create" or change.operation_name is not None
                    or resource.resource_type != "signoffs" or resource.resource_id != signoff_id
                    or resource.business_id != business or resource.namespace is not None
                    or plain(change.values) != values or expected.resource != resource or expected.exists
                    or expected.revision is not None or expected.content_hash is not None):
                raise ValueError("question effect mismatch")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DomainError("invalid_plan", "The stored input request is not canonical") from exc


@contextmanager
def _read_only(connection, database):
    """The caller's own snapshot, query-only, for one proof.

    The target ``Database`` has never been opened (the store is still the predecessor, which the target correctly refuses to
    open), so it learns the column catalog of the caller's snapshot: every target table is present in it with its target
    columns. Nothing is validated, created or written."""
    previous_rows = connection.row_factory
    previous = connection.execute("PRAGMA query_only").fetchone()[0]
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    database._remember_columns(connection)
    try:
        yield
    finally:
        connection.execute(f"PRAGMA query_only = {'ON' if previous else 'OFF'}")
        connection.row_factory = previous_rows


def prove_receipt(target, connection, receipt_id: str) -> None:
    """The executor's pre-resume validation of one pending keyed receipt, read-only; raises ``DomainError`` when blocked."""
    runtime = target.runtime
    if runtime is None:
        raise DomainError("unverified_pending_plan", "The target composition is not available to prove the saved plan")
    application = runtime.application
    with _read_only(connection, runtime.database):
        uow = UnitOfWork(runtime.database, connection, writable=False)
        row = ApplyReceipts(uow).get(receipt_id)
        if row["signoff_id"] is not None:
            return _prove_signoff(application, connection, uow, row["signoff_id"])
        invocation = (row["progress_json"] or {}).get("invocation")
        if not isinstance(invocation, dict) or not isinstance(invocation.get("input_hash"), str):
            raise DomainError("unverified_pending_plan", "The saved receipt has no durable invocation binding")
        plan = PreparedFileExecutor._plan(row, invocation["input_hash"])
        business = (plan.target_namespace.removeprefix("business:")
                    if plan.target_namespace.startswith("business:") else None)
        context = application.policy.context_for(
            plan.principal_id, business_id=business, review_business_id=plan.review_business_id,
            initiated_by_principal_id=plan.initiated_by_principal_id, job_id=plan.policy_binding.get("job_id"), uow=uow)
        descriptor = application.registry.get(plan.operation_name)
        options = InvocationOptions.from_mapping(invocation["options"])
        expected_hash = canonical_hash({"operation": descriptor.name, "version": descriptor.version,
            "arguments": plain(plan.arguments), "options": {**plain(options), "business_id": business,
            "review_business_id": plan.review_business_id, "idempotency_key": None}})
        if (not options.idempotency_key or invocation.get("idempotency_key") != options.idempotency_key
                or PreparedFileExecutor._id(context, plan.target_namespace, descriptor.name, options) != receipt_id
                or invocation["input_hash"] != expected_hash):
            raise DomainError("invalid_plan", "The saved receipt cannot resume the same keyed invocation")
        application.validate_prepared(context, plan, uow, receipt_id=receipt_id, allow_unverified_files=True)


def prove_signoff(target, connection, signoff_id: str) -> None:
    runtime = target.runtime
    if runtime is None:
        raise DomainError("unverified_pending_plan", "The target composition is not available to prove the saved plan")
    with _read_only(connection, runtime.database):
        _prove_signoff(runtime.application, connection, UnitOfWork(runtime.database, connection, writable=False), signoff_id)


def _prove_signoff(application, connection, uow, signoff_id: str) -> None:
    row = decode_row(connection.execute("SELECT * FROM signoffs WHERE id = ?", (signoff_id,)).fetchone())
    retryable = (row is not None and row["state"] == "failed" and (row["error_json"] or {}).get("retryable") is True
                 and (row["decision_json"] or {}).get("kind") == "approve")
    if row is None or (row["state"] not in ("pending", "approved") and not retryable):
        raise DomainError("unverified_pending_plan", "The saved Signoff is not a pending or approved proposal")
    if application.signoffs is None:
        raise DomainError("unverified_pending_plan", "The target composition has no Signoff service")
    if retryable:
        # The service's retry inspection deliberately leaves the failed row untouched. It only returns to approved
        # during a later, explicit owner retry; migration must not simulate that state transition.
        plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
        context = application.signoffs._proposer_context(plan, uow)
        application.validate_prepared(context, plan, uow, receipt_id=row["id"],
                                      allow_unverified_files=True, require_approval=False)
    else:
        application.signoffs._validate(row, uow, approved=row["state"] == "approved")
