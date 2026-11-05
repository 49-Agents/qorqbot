"""The final registered Context filing programs: ``context.file`` and ``context.instruct``.

Both are completion programs. The upload, note, journal, accepted learning or instruction already is a frozen source
version, a registered job and a GENERAL queue input (admitted in one prepared operation); the backend Context driver alone
starts propagation. A job therefore never calls a model, reads a document body or creates a queue input. Its whole run is:

* While the bound input is queued, processing, blocked or waiting for a discard, or its terminal receipt has not been
  recorded yet, return ``ProgramPending`` (15 seconds) so the retained serial executor is released.
* Once the stored terminal receipt exists, freeze exactly one declared output, ``context_filing_receipt`` with the receipt's
  stable ID, and (for a discarded input) the ``completion_error`` that makes the job settle as non-success.

Nothing is taken from arguments but the filing ID: the filing, receipt, job, program and worker are read back from the
database, so a program cannot be pointed at another filing's receipt. The definitions are new (``context.file`` version 2,
``context.instruct`` version 1): a job frozen under the old filing, summary, map or base definitions never runs here.
"""

from __future__ import annotations

from qorqut_operations.application.plans import UUID, schema
from qorqut_operations.background.programs import Program, ProgramPending, ProgramResult
from qorqut_context.integration.filing import completion
from qorqut_operations.contracts import DomainError

FILE = "context.file"
INSTRUCT = "context.instruct"
RECEIPT = "context_filing_receipt"
RETRY_AFTER = 15
ARGUMENTS = schema({"filing_id": UUID}, ["filing_id"])
FILE_RESOURCES = ("documents", "managed_document_files", "context_completions")
INSTRUCT_RESOURCES = ("context_completions",)
MESSAGE_LIMIT = 500


class FilingPrograms:
    """Both programs share one run: they differ only in the source they complete."""

    def run(self, control, args):
        job = control.job
        with control.service.database.read() as uow:
            bound = completion.filing(uow, args["filing_id"])
            if bound is None or bound["job_id"] != job["id"] or bound["program_id"] != job["kind"] or bound["business_id"] != job["business_id"]:
                raise DomainError("forbidden", "This job is not the registered job of that Context filing")
            stored = completion.receipt_for_filing(uow, bound["id"])
        control.check()
        if stored is None:
            return ProgramPending(retry_after=RETRY_AFTER)
        data = {"filed": stored["outcome"] == "published", "filing_id": bound["id"], "source_revision": bound["source_revision"],
                "context_event_id": stored["context_event_id"], "published_commit": stored["published_commit"],
                "document_id": bound["source_id"] if bound["source_kind"] == "document" else None}
        error = None
        if stored["outcome"] == "discarded":
            saved = stored["reason_json"] or {}
            reason = saved.get("reason") or (saved.get("request_evidence") or {}).get("reason") or saved.get("context") or "discarded"
            error = {"code": "context_discarded", "message": ("The Context input was discarded: " + str(reason))[:MESSAGE_LIMIT],
                     "retryable": False}
        return ProgramResult(outputs=({"operation": RECEIPT, "arguments": {"receipt_id": stored["id"]}},), data=data,
                             completion_error=error)

    def register(self, programs):
        """The two final definitions: the filing and instruction completion programs, with only the receipt as output."""
        run = self.run
        programs.register(Program(FILE, 2, ARGUMENTS, run, {RECEIPT: "allow"}, (RECEIPT,), FILE_RESOURCES,
                                  scope_kinds=("business", "global"), global_capabilities=("read_global_context",), background_queue="context"))
        programs.register(Program(INSTRUCT, 1, ARGUMENTS, run, {RECEIPT: "allow"}, (RECEIPT,), INSTRUCT_RESOURCES,
                                  scope_kinds=("business", "global"), global_capabilities=("read_global_context",), background_queue="context"))
        return programs
