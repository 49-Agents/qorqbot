"""Durable execution of one owner-approved personal WhatsApp message.

The job exists only because a ``whatsapp_send`` Signoff was approved by the owner and applied; this program refuses any
job that cannot be traced to exactly that Signoff, so a routine, a generic job or a forged definition cannot send. It
holds no approval logic of its own: it rechecks the agent, binding, account and approved plan, then submits the message
once, under a request ID derived from the job, to the connector (which reserves that ID durably before sending and never
resends a recorded one).

Recovery never creates a new request ID and never resubmits after an uncertain outcome: once the dispatch intent is
checkpointed, every later attempt only asks the connector for its receipt. An outcome that stays unknown ends the job as
``external_unconfirmed``; it is never reported as sent and never sent again by this program.
"""

from __future__ import annotations

from qorqut_operations.background.programs import ProgramPending, ProgramResult
from qorqut_operations.contracts import DomainError, plain

from .client import (ACCOUNT_REF, UnknownOutcome, check_receipt, payload_fingerprint,
                                            request_id_for_job, validate_payload)

OPERATION = "whatsapp_send"
ATTEMPTS = 8
FIELDS = {"scope", "account_ref", "chat_id", "text"}


class WhatsAppSendProgram:
    def __init__(self, operations):
        self.operations = operations

    def run(self, control, args):
        return self._dispatch(control, args)

    # The engine hands attempts after the first to ``recover``. Recovery here is decided by the durable dispatch intent,
    # never by the attempt count: an attempt that never reached the connector may still submit, one that did may not.
    recover = run

    def _dispatch(self, control, args):
        if self.operations is None:
            return self._pending(control, "Managed personal WhatsApp sending is not installed")
        if not isinstance(args, dict) or set(args) != FIELDS or args["scope"] != "global" or control.job["business_id"] is not None:
            raise DomainError("invalid_plan", "WhatsApp send job is malformed")
        chat_id, text, account_ref = args["chat_id"], args["text"], args["account_ref"]
        validate_payload(chat_id, text)
        if not isinstance(account_ref, str) or ACCOUNT_REF.fullmatch(account_ref) is None:
            raise DomainError("invalid_plan", "WhatsApp send job has no frozen account identity")
        request_id = request_id_for_job(control.job["id"])
        fingerprint = payload_fingerprint(chat_id, text)
        bind = dict(request_id=request_id, chat_id=chat_id, text=text, account_ref=account_ref)
        stored = control.saved("external_receipt")
        if stored is not None:
            # A checkpoint is durable input but still external data: rebind it to this job before completing.
            if (not isinstance(stored, dict) or stored.get("request_id") != request_id or stored.get("account_ref") != account_ref
                    or stored.get("fingerprint") != fingerprint or stored.get("state") != "accepted"):
                raise DomainError("external_result_invalid", "The saved WhatsApp receipt does not match this job")
            return ProgramResult(data={"operation": OPERATION, "receipt": plain(stored)})
        if control.saved("dispatch_intent") is not None:
            return self._recover(control, bind)
        with control.service.database.read() as uow:
            settings = self._authorize(control, args, uow)
        client = self.operations.client(settings)
        control.check()
        # Nothing has been attempted yet. A connector that is down or disconnected proves nothing was sent, so wait
        # without recording an intent; the submission below is the only point of no return.
        try:
            account = client.account()
        except DomainError as exc:
            if exc.code == "operation_unavailable":
                return self._pending(control, "The personal WhatsApp connector is unavailable")
            raise
        if account["account_ref"] != account_ref:
            raise DomainError("forbidden", "The connected WhatsApp account differs from the approved account; nothing was sent")
        if not account["connected"]:
            return self._pending(control, "WhatsApp is disconnected")
        with control.service.database.transaction() as uow:
            control.checkpoint("dispatch_intent", {"request_id": request_id, "account_ref": account_ref,
                                                   "fingerprint": fingerprint}, uow=uow)
        try:
            receipt = client.send(request_id, chat_id, text, account_ref)
        except UnknownOutcome:
            return self._pending(control, "The WhatsApp send outcome is not yet known")
        return self._settle(control, check_receipt(receipt, **bind))

    def _recover(self, control, bind):
        """After a dispatch intent: receipt-only. Never resubmits, whatever the connector or the clock says."""
        settings = self.operations.settings()
        if settings is None:
            return self._pending(control, "Managed personal WhatsApp sending is not installed")
        control.check()
        try:
            receipt = self.operations.client(settings).receipt(bind["request_id"], bind["account_ref"])
        except DomainError as exc:
            if exc.code == "operation_unavailable":
                return self._pending(control, "The WhatsApp receipt lookup is unavailable")
            raise
        if receipt is None:
            return self._pending(control, "The WhatsApp send has no durable connector receipt")
        return self._settle(control, check_receipt(receipt, **bind))

    def _settle(self, control, receipt):
        if receipt["state"] == "accepted":
            control.checkpoint("external_receipt", receipt)
            return ProgramResult(data={"operation": OPERATION, "receipt": receipt})
        return self._pending(control, "The WhatsApp send outcome is unconfirmed")

    @staticmethod
    def _pending(control, message):
        if control.job.get("attempts", 0) >= ATTEMPTS:
            raise DomainError("external_unconfirmed", message + " after bounded recovery; it was not repeated", retryable=False)
        return ProgramPending(retry_after=15)

    def _authorize(self, control, args, uow):
        """Recheck, immediately before any dispatch, everything the approval relied on."""
        job = control.job
        denied = DomainError("forbidden", "This WhatsApp send job is not backed by an approved owner Signoff")
        marker = (job.get("definition_ref_json") or {}).get(OPERATION)
        if marker != {"operation": OPERATION, "agent_id": job.get("initiated_by_principal_id")}:
            raise denied
        app = control.service.app
        context = app.policy.context_for(job["initiated_by_principal_id"], business_id=None, review_business_id=None, uow=uow)
        app.authorize(context, app.registry.get(OPERATION), (), uow, namespace="global")
        settings = self.operations._access(context, uow)
        if settings["account_ref"] != args["account_ref"]:
            raise DomainError("forbidden", "The personal WhatsApp installation changed since approval; nothing was sent")
        self._approved(job, args, uow, denied)
        return settings

    @staticmethod
    def _approved(job, args, uow, denied):
        from qorqut.background.approved_send import approved_send_plan
        plan = approved_send_plan(job, args, OPERATION, uow)
        if plan.arguments.get("chat_id") != args["chat_id"] or plan.arguments.get("text") != args["text"]:
            raise denied
