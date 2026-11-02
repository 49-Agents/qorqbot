"""Durable, at-most-once execution of one named Fastmail account mutation.

The queued job's id is the engine's request id. The program records its dispatch intent durably *before* the single POST, and
from then on only ever reads the engine's receipt: a restart, a timeout or a lost response never posts the action again. A
receipt in ``prepared`` or ``uncertain`` state keeps the job pending; after a bounded number of attempts the job ends as
``external_unconfirmed`` for the owner to inspect, never as a guess of success or failure."""

from __future__ import annotations

import re

from qorqut_operations.background.programs import ProgramPending, ProgramResult
from qorqut_operations.contracts import DomainError, plain

from .client import action_digest
from .operations import ACTIONS, MUTATIONS, PROGRAM, native_payload

MAX_ATTEMPTS = 8
RETRY_SECONDS = 15
# Engine and configuration conditions that mean "not reached or not answered", never "refused".
PENDING_CODES = frozenset({"operation_unavailable", "email_unavailable", "provider_unavailable"})
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["scope", "account_id", "operation", "arguments"],
          "properties": {"scope": {"const": "global"}, "account_id": {"type": "string", "minLength": 1, "maxLength": 200},
                         "operation": {"enum": sorted(MUTATIONS)}, "arguments": {"type": "object"}}}


class FastmailMutationProgram:
    def __init__(self, provider):
        self.provider = provider

    def run(self, control, args):
        return self._dispatch(control, args, recover=False)

    def recover(self, control, args):
        return self._dispatch(control, args, recover=True)

    def _dispatch(self, control, args, *, recover):
        if args.get("scope") != "global" or control.business_id:
            raise DomainError("invalid_plan", "Fastmail mutation job has an invalid scope")
        account_id = args.get("account_id")
        operation, payload = args.get("operation"), args.get("arguments")
        if not isinstance(account_id, str) or not account_id:
            raise DomainError("invalid_plan", "Fastmail mutation job has no frozen account identity")
        if not isinstance(operation, str) or not isinstance(payload, dict) or operation not in ACTIONS:
            raise DomainError("invalid_plan", "Fastmail mutation job is malformed or names an unknown operation")
        action = ACTIONS[operation]
        with control.service.database.read() as uow:
            self._authorize(control, args, uow)
        client = self.provider.client()
        try:
            client.token      # an unreadable engine token is "not configured now", before any intent is recorded
        except DomainError as exc:
            if exc.code not in PENDING_CODES:
                raise
            if control.saved("dispatch_intent") is None:
                return self._not_sent(control, "The Fastmail engine is not configured")
            return self._pending(control, "The Fastmail engine is not configured")
        request = {**native_payload(operation, payload), "account_id": account_id}
        expected_digest = action_digest(control.job["id"], action, request)
        if not recover and control.saved("dispatch_intent") is not None:
            recover = True
        receipt = control.saved("external_receipt")
        # Pending provider states are not completion receipts: re-read the engine ledger so a late completion is observed.
        if isinstance(receipt, dict) and receipt.get("state") in {"prepared", "uncertain"}:
            receipt, recover = None, True
        if receipt is None:
            control.check()
            if recover:
                try:
                    receipt = client.receipt(control.job["id"])
                except DomainError as exc:
                    if exc.code in PENDING_CODES:
                        return self._pending(control, "The Fastmail receipt lookup is unavailable")
                    raise
                if receipt is None:
                    return self._pending(control, "The Fastmail action has no durable receipt")
                intent = control.saved("dispatch_intent") or {}
                self._validate_receipt(receipt, control.job["id"], action, intent.get("account_id", account_id), expected_digest)
                return self._receipt_result(control, operation, receipt)
            with control.service.database.transaction() as uow:
                control.checkpoint("dispatch_intent", {"operation": operation, "action": action, "account_id": account_id,
                                                       "digest": expected_digest}, uow=uow)
            try:
                receipt = client.mutation(control.job["id"], action, request)
            except DomainError as exc:
                if exc.code in PENDING_CODES:
                    return self._pending(control, "The Fastmail action dispatch is unconfirmed")
                raise
            self._validate_receipt(receipt, control.job["id"], action, account_id, expected_digest)
            if receipt.get("state") in {"completed", "failed"}:
                control.checkpoint("external_receipt", plain(receipt))
        else:
            # A terminal checkpoint is durable input but still external data: rebind it to this job and action.
            self._validate_receipt(receipt, control.job["id"], action, account_id, expected_digest)
        return self._receipt_result(control, operation, receipt)

    @staticmethod
    def _authorize(control, args, uow):
        """The initiating principal's current authority over exactly this operation and these arguments, rechecked at run."""
        operation, payload = args["operation"], args["arguments"]
        if control.job.get("definition_ref_json", {}).get("fastmail_operation") != operation:
            raise DomainError("forbidden", "Fastmail job has no bound operation")
        app = control.service.app
        ctx = app.policy.context_for(control.job["initiated_by_principal_id"], business_id=None, review_business_id=None, uow=uow)
        app.authorize(ctx, app.registry.get(operation), (), uow, namespace="global")
        app.registry.bind_arguments(operation, payload)
        app.fastmail.access(ctx, uow)

    @staticmethod
    def _pending(control, message):
        if control.job.get("attempts", 0) >= MAX_ATTEMPTS:
            raise DomainError("external_unconfirmed", message + " after bounded recovery", retryable=False)
        return ProgramPending(retry_after=RETRY_SECONDS)

    @staticmethod
    def _not_sent(control, message):
        """Nothing was dispatched yet, so a bounded failure here is a plain, definite refusal rather than an unconfirmed outcome."""
        if control.job.get("attempts", 0) >= MAX_ATTEMPTS:
            raise DomainError("provider_unavailable", message + "; nothing was sent", retryable=False)
        return ProgramPending(retry_after=RETRY_SECONDS)

    def _receipt_result(self, control, operation, receipt):
        state = receipt.get("state")
        if state in {"uncertain", "prepared"}:
            return self._pending(control, "The Fastmail action remains unconfirmed")
        if state == "failed":
            raise DomainError("external_failed", "Fastmail rejected the account action")
        if state == "completed":
            return ProgramResult(data={"operation": operation, "receipt": plain(receipt)})
        raise DomainError("external_result_invalid", "The Fastmail engine returned a non-terminal mutation receipt")

    @staticmethod
    def _validate_receipt(receipt, request_id, action, account_id=None, expected_digest=None):
        if not isinstance(receipt, dict) or receipt.get("request_id") != request_id:
            raise DomainError("external_result_invalid", "The Fastmail engine returned an invalid mutation receipt")
        if receipt.get("action") != action:
            raise DomainError("external_result_invalid", "The Fastmail engine returned a receipt for another action")
        if account_id is not None and receipt.get("account_id") != account_id:
            raise DomainError("external_result_invalid", "The Fastmail engine returned a receipt for another account")
        digest = receipt.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise DomainError("external_result_invalid", "The Fastmail engine returned a receipt without a valid digest")
        if expected_digest is not None and digest != expected_digest:
            raise DomainError("external_result_invalid", "The Fastmail engine returned a receipt for another request")
        if receipt.get("state") not in {"prepared", "completed", "failed", "uncertain"}:
            raise DomainError("external_result_invalid", "The Fastmail engine returned an unknown mutation state")


def register(programs, provider) -> FastmailMutationProgram:
    """Register the one named program on the open program registry (global scope, external, recoverable, never a routine)."""
    from qorqut_operations.background.programs import Program
    program = FastmailMutationProgram(provider)
    programs.register(Program(PROGRAM, 1, SCHEMA, program.run, {}, (), (), global_capabilities=("manage_fastmail",),
                              scope_kinds=("global",), external=True, recover=program.recover, timeout_seconds=120,
                              routine_allowed=False))
    return program
