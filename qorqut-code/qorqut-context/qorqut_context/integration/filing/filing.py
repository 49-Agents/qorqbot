"""The host side of Context filing: frozen source documents, admission, retry and completion for uploads and instructions.

The Operations ``context_filing`` contribution owns source versions, filings, the terminal outcome outbox and the queue
input; this module is the document and instruction adapter over it. Everything it submits to Context is one canonical
``qorqut-source-v1`` document (``context_filing.envelope``): the exact body plus an explicit, allowlisted ``metadata`` object.

The metadata allowlist is exact and closed (a field not named here cannot reach Context or change a source's digest).

Documents (``context_upload`` rows: raw uploads, note and journal snapshots, accepted learnings):

* ``title``, ``document_kind`` (the registered kind), ``source`` (the stated source: ``upload``, ``notes``,
  ``global-journal``, ``accepted-learning``), ``date`` (the document's stated date), ``format`` (``notes-v1`` /
  ``learning-v1``), ``uploaded_by`` (the principal that created the document);
* ``origin`` and ``occurred_on`` for a call or meeting, and ``participants``: the normalized list of ``{name, person_id?}``
  (blank names refused, names stripped, a Person's registered name resolved under the caller's source-read authority at
  admission and frozen);
* the note/journal/learning attribution and reference fields: ``source_snapshot_hash``, ``learning_tag``, ``journal_source_id``,
  ``journal_source_kind``, ``journal_source_business_id``, ``journal_source_hash``, ``journal_source_date``,
  ``journal_uploader_id`` and ``references`` (the document's ``source_refs``).

Excluded, so that bookkeeping never changes a source: ``status``, ``error``, ``attempts``, ``last_attempt``, ``processed``,
``filed_by``, ``result``, ``context_base``, ``context_filing`` and every receipt field, the mutable document row revision,
``source_revision`` and ``journal_source_revision`` (row revisions of the originating note), ``journal_uploaded_at``,
``uploaded``, ``upload_id``, ``updated`` and any upload or retry timestamp.

Instructions (``context.instruct``): ``title``, ``document_kind`` (``instruction``), ``source``, ``instructed_by`` (the
attributed principal, kind, display name and role), ``scope``, ``rationale`` and ``people`` (the names the instruction gave).

The body is submitted exactly as supplied; nothing is truncated. A source whose envelope exceeds the Context library's
source-byte limit is refused before admission with an error that says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from qorqut_context import input_modes as modes
from qorqut_context.integration import filing as context_filing
from qorqut_operations.application.plans import prepare_job
from qorqut_context.integration.filing import completion, envelope
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.storage.repositories import decode_row

from qorqut_operations.documents.serialization import split_document

FILE_PROGRAM = "context.file"
INSTRUCT_PROGRAM = "context.instruct"
DOCUMENT_KINDS = frozenset({"context_upload"})
# Every kind whose reads carry the one current-source filing projection. A ``context_dump`` is presented the same way, but it
# is not an uploadable source: no operation files, retries or retracts it, so it is never offered as retryable.
PRESENTED_KINDS = DOCUMENT_KINDS | {"context_dump"}
RECEIPT_OPERATION = "context_filing_receipt"

# Stored document metadata key -> envelope metadata key (a straight copy; absent or null values are not sent).
DOCUMENT_FIELDS = {
    "title": "title", "source": "source", "date": "date", "format": "format", "by": "uploaded_by", "origin": "origin",
    "occurred_on": "occurred_on", "source_snapshot_hash": "source_snapshot_hash", "learning_tag": "learning_tag",
    "journal_source_id": "journal_source_id", "journal_source_kind": "journal_source_kind",
    "journal_source_business_id": "journal_source_business_id", "journal_source_hash": "journal_source_hash",
    "journal_source_date": "journal_source_date", "journal_uploader_id": "journal_uploader_id"}
DOCUMENT_METADATA = ("document_kind", "participants", "references", *DOCUMENT_FIELDS.values())
INSTRUCTION_METADATA = ("title", "document_kind", "source", "instructed_by", "scope", "rationale", "people")
# Document bookkeeping that never reaches the envelope; listed for the record and checked by tests.
EXCLUDED_BOOKKEEPING = ("status", "error", "attempts", "last_attempt", "processed", "filed_by", "result", "context_base",
                        "context_filing", "source_revision", "journal_source_revision", "journal_uploaded_at", "uploaded",
                        "upload_id", "updated", "content_owner", "summary_owner")
ERROR_LIMIT = 300


@dataclass(frozen=True)
class FilingPorts:
    """The explicit, typed filing dependency of ``DocumentOperations``: the queue's own control preparation (the one way an
    explicit retry is requested) and the Context library's source-byte limit."""

    queue: object
    source_bytes: int = modes.SOURCE_BYTES

    def __post_init__(self):
        if not callable(getattr(self.queue, "prepare_control", None)):
            raise TypeError("Context filing needs the Context queue operations")
        if type(self.source_bytes) is not int or self.source_bytes < 1:
            raise ValueError("The Context source limit is a positive number of bytes")


def document_metadata(kind: str, metadata: dict, source_refs, participants) -> dict:
    """The allowlisted metadata of one document source. A pure function of the document's stored facts."""
    result = {"document_kind": kind}
    for stored, name in DOCUMENT_FIELDS.items():
        if metadata.get(stored) is not None:
            result[name] = metadata[stored]
    if participants is not None:
        result["participants"] = participants
    if source_refs:
        result["references"] = plain(source_refs)
    return result


def event_metadata(metadata: dict, identity: str) -> dict:
    """The Context event's own source metadata: kind, title and origin, never a timestamp of the upload or retry."""
    return {"kind": "document", "title": str(metadata.get("title") or "Context upload"),
            "origin": str(metadata.get("source") or "upload"), "origin_ref": "document:" + identity}


def error_summary(error) -> dict | None:
    if not isinstance(error, dict):
        return None
    return {"code": str(error.get("code") or "error"), "message": str(error.get("message") or "")[:ERROR_LIMIT]}


class ContextFiling:
    def __init__(self, store, programs, ports: FilingPorts):
        if not isinstance(ports, FilingPorts):
            raise TypeError("Context filing needs its typed FilingPorts")
        self.store, self.programs, self.ports = store, programs, ports

    # -- frozen sources --------------------------------------------------------------------------------------------------

    def participants(self, uow, metadata: dict, business_id, ctx=None):
        """The normalized participants. With ``ctx`` (admission) a Person is read under that caller's source-read authority
        and its registered name is frozen; without it (recomputing the current digest) the same name is read without
        disclosing anything, so the digest never depends on who asks."""
        if "participants" not in metadata:
            return None
        normalized = []
        for item in metadata["participants"] or []:
            name, identity = str(item.get("name") or "").strip(), item.get("person_id")
            if identity:
                person = (self.store.get(ctx, identity, uow, kinds={"context_person"}) if ctx is not None
                          else uow.repository("documents", business_id=business_id).get(identity))
                if person is not None:
                    name = str(person["metadata_json"].get("name") or name).strip()
            if not name:
                raise DomainError("validation_error", "Participant name cannot be blank")
            normalized.append({"name": name, **({"person_id": identity} if identity else {})})
        return normalized

    def document_source(self, uow, *, kind, body, metadata, source_refs, business_id, ctx=None) -> str:
        return envelope.source_document(body, document_metadata(kind, metadata, source_refs,
                                                                self.participants(uow, metadata, business_id, ctx)))

    def current_digest(self, uow, row) -> str:
        """The digest of the document as it is now, from its stored body and allowlisted metadata (no status or receipt)."""
        body = split_document(self.store.read_bytes(row, uow))[1]
        return envelope.fingerprint(self.document_source(uow, kind=row["kind"], body=body, metadata=row["metadata_json"],
                                                         source_refs=row["source_refs_json"], business_id=row["business_id"]))

    def digest_of(self, uow, bound) -> str | None:
        """The current digest of a filing's source: the document as it is now, or the instruction's own frozen version."""
        if bound["source_kind"] == "instruction":
            return bound["source_digest"]
        row = uow.repository("documents", business_id=bound["business_id"]).get(bound["source_id"])
        return None if row is None or row.get("archived") else self.current_digest(uow, row)

    # -- admission -------------------------------------------------------------------------------------------------------

    def _admit(self, builder, uow, *, kind, source_id, program, document, event, retracts=None):
        filing_id = context_filing.new_filing_id()
        job_id = prepare_job(builder, uow, program, {"filing_id": filing_id}, programs=self.programs)
        return context_filing.admit(builder, uow, kind=kind, source_id=job_id if kind == "instruction" else source_id,
                                    filing_id=filing_id, job_id=job_id, document=document, event_metadata=event,
                                    retracts=retracts, max_content_bytes=self.ports.source_bytes,
                                    business_id=builder.ctx.business_id)

    def admit_document(self, builder, uow, *, identity, kind, body, metadata, source_refs=(), retracts=None):
        """The next source version of one document, its registered job and its queue input, in the caller's plan."""
        document = None if retracts is not None else self.document_source(
            uow, kind=kind, body=body, metadata=metadata, source_refs=source_refs, business_id=builder.ctx.business_id, ctx=builder.ctx)
        return self._admit(builder, uow, kind="document", source_id=identity, program=FILE_PROGRAM, document=document,
                           event=event_metadata(metadata, identity), retracts=retracts)

    def admit_current(self, builder, uow, row):
        """A new version of an existing document, from its current stored file."""
        body = split_document(self.store.read_bytes(row, uow))[1]
        return self.admit_document(builder, uow, identity=row["id"], kind=row["kind"], body=body, metadata=row["metadata_json"],
                                   source_refs=row["source_refs_json"])

    def admit_instruction(self, builder, uow, args, attribution: dict):
        """A focused instruction: its own source (the job's) frozen with the caller's attribution."""
        ctx = builder.ctx
        metadata = {"title": "Context instruction", "document_kind": "instruction", "source": "instruction",
                    "instructed_by": plain(attribution), "scope": "business" if ctx.business_id else "global"}
        if args.get("rationale"):
            metadata["rationale"] = args["rationale"]
        if args.get("people"):
            metadata["people"] = list(args["people"])
        return self._admit(builder, uow, kind="instruction", source_id="", program=INSTRUCT_PROGRAM,
                           document=envelope.source_document(args["text"], metadata),
                           event={"kind": "instruction", "title": metadata["title"], "origin": "instruction",
                                  "origin_ref": "principal:" + str(attribution["principal_id"])})

    # -- reads -----------------------------------------------------------------------------------------------------------

    @staticmethod
    def _redact(ctx, shown: dict, job) -> dict:
        """Other agents get status and error codes for coordination, never the frozen reason text."""
        if ctx.principal_kind == "agent" and (job is None or job["initiated_by_principal_id"] != ctx.principal_id):
            error = shown.get("error")
            return {**shown, "error": None if error is None else {"code": error["code"]}}
        return shown

    @staticmethod
    def _job(uow, shown):
        """The filing's registered job row (decoded), or None when no job is bound."""
        if shown.get("job_id") is None:
            return None
        return decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (shown["job_id"],)).fetchone())

    def document_filing(self, uow, ctx, row) -> tuple[dict, str]:
        """``(filing, status)``: the one current-source projection of a document and its derived summary.

        ``filing`` is the existing completion object (``completion.applicable``: filing, source revision, input, job, exact queue
        state, published commit, completion state, ``source_current``, error and queue position) plus the read-only display facts
        ``document_revision``, ``operation``, ``status``, ``attempts``, ``job_state`` and ``retryable``. A source that no accepted
        filing matches reads ``unfiled`` with no identifiers, never the last successful filing, and a historical publication never
        marks newer content processed. The summary is derived from this and from nothing the document row stores."""
        digest = None if row.get("archived") else self.current_digest(uow, row)
        shown = completion.applicable(uow, "document", row["id"], digest)
        job = self._job(uow, shown)
        status = self.status_of(uow, shown)
        facts = {**shown, "document_revision": row["revision"], "operation": self._operation(uow, shown), "status": status,
                 "attempts": self._attempts(uow, shown), "job_state": None if job is None else job["state"],
                 "retryable": self._retryable(uow, row, shown, job)}
        return self._redact(ctx, facts, job), status

    def job_filing(self, uow, ctx, job) -> dict | None:
        bound = completion.filing_for_job(uow, job["id"])
        if bound is None:
            return None
        shown = completion.describe(uow, bound["id"], current_digest=self.digest_of(uow, bound))
        return self._redact(ctx, shown, job)

    @staticmethod
    def _operation(uow, shown: dict) -> str | None:
        """The accepted source operation (``add``, ``edit`` or ``remove``) of the applicable filing; null when unfiled."""
        if shown["filing_id"] is None:
            return None
        return uow.connection.execute(
            "SELECT v.operation FROM context_filings f JOIN context_source_versions v ON v.id = f.source_version_id WHERE f.id=?",
            (shown["filing_id"],)).fetchone()["operation"]

    @staticmethod
    def _attempts(uow, shown: dict) -> int:
        """Recorded attempts of the filing's input: zero exactly when it has none (including an unfiled source)."""
        if shown["input_id"] is None:
            return 0
        return uow.connection.execute("SELECT COUNT(*) AS n FROM context_input_attempts WHERE input_id=?",
                                      (shown["input_id"],)).fetchone()["n"]

    @staticmethod
    def status_of(uow, shown: dict) -> str:
        """The pending/processed/failed (or retracted) summary the applicable filing implies. Old frontmatter never decides.

        Queued, processing, blocked, discard requested, unfiled and published-with-completion-pending are all pending (a blocked
        input keeps its explicit error and exact state). Published and applied is processed, or retracted for a removal. A
        discarded input is failed, and so is a completion conflict (its published commit stays on the filing)."""
        state, done = shown["state"], shown["completion_state"]
        if state == "published":
            if done == "applied":
                operation = ContextFiling._operation(uow, shown)
                return "retracted" if operation == "remove" else "processed"
            return "failed" if done == "conflict" else "pending"
        return "failed" if state == "discarded" else "pending"

    def _retryable(self, uow, row, shown: dict, job) -> bool:
        """Whether ``retry`` can meaningfully progress this source now: a display hint, never authority or a permission check."""
        if row["kind"] not in DOCUMENT_KINDS or row.get("archived"):
            return False
        return self.retry_branch(uow, row, shown, job)[0] in ("admit", "control", "requeue")

    # -- retry -----------------------------------------------------------------------------------------------------------

    def retry_branch(self, uow, row, shown: dict, job) -> tuple[str, object]:
        """Which existing retry branch applies to a document's current source, and what it needs.

        ``admit`` (the source has no accepted filing), ``control`` (a blocked input to retry through the queue's own control, with
        none pending), ``pending_control`` (that control is already pending: no second offer), ``requeue`` (a bound job that failed
        before its completion applied), ``discarded`` and ``stopped`` (never revived), ``definition_changed`` (the registered
        program no longer matches the job's frozen definition), or ``none``. Both ``retry`` and the read-only ``retryable`` hint use
        this one decision, so the hint cannot offer what the retry would refuse."""
        if shown["state"] == "unfiled":
            return "admit", None
        if shown["state"] == "discarded":
            return "discarded", None
        if job["state"] in completion.STOPPED_JOB_STATES:
            return "stopped", None
        if shown["state"] == "blocked":
            pending = uow.connection.execute("SELECT 1 FROM context_input_controls WHERE input_id=? AND status='pending'",
                                             (shown["input_id"],)).fetchone()
            return ("pending_control" if pending else "control"), None
        stored = completion.receipt_for_filing(uow, shown["filing_id"])
        if job["state"] == "failed" and (stored is None or stored["state"] != "applied"):
            program = self.programs.get(job["kind"])
            if program is None or program["digest"] != (job["definition_ref_json"] or {}).get("digest"):
                return "definition_changed", None
            return "requeue", program
        return "none", None

    def retry(self, builder, uow, ctx, row, options, merge):
        """Move a document's current source forward without another model input for a completion-only failure.

        An unfiled current source (never filed, or changed since) is admitted as its next version. A blocked input is
        retried through the queue's own control. A bound job that failed before its completion applied is requeued with its
        frozen outputs and receipt intact, after its worker and frozen definition are checked again. A discarded filing
        cannot be revived, and neither can a cancelled job."""
        digest = self.current_digest(uow, row)
        shown = completion.applicable(uow, "document", row["id"], digest)
        job = self._job(uow, shown)
        branch, _ = self.retry_branch(uow, row, shown, job)
        if branch == "admit":
            self.admit_current(builder, uow, row)
            builder.effects.append("The current document differs from every accepted version, so it is queued as a new source version.")
        elif branch == "discarded":
            raise DomainError("invalid_transition", "This Context input was discarded and cannot be retried; change the source to "
                              "submit a new version")
        elif branch == "stopped":
            raise DomainError("invalid_transition", "The registered job was cancelled; cancellation is not undone by a retry")
        elif branch == "control":
            merge(builder, self.ports.queue.prepare_control(
                "retry", ctx, {"input_id": shown["input_id"], "expected_state": "blocked"}, options, uow), "context_retry")
        elif branch == "definition_changed":
            raise DomainError("forbidden", "The registered program definition changed; this job can no longer apply its completion")
        elif branch == "requeue":
            self.programs.authority.require_worker(job["principal_id"], job["business_id"], uow)
            builder.record("jobs", job["id"], job, {"state": "queued", "error_json": None, "next_retry_at": None, "claimed_by": None,
                                                    "lease_until": None}, business_id=job["business_id"], label="Requeue the filing job")
            builder.effects.append("Requeues the same registered job; its frozen outputs and receipts are kept.")

    # -- completion ------------------------------------------------------------------------------------------------------

    def prepare_receipt(self, builder, uow, args):
        """The worker-only completion: re-resolve everything from the stored receipt, then apply the source bookkeeping and
        the receipt's applied marker in one plan. The caller supplies nothing but the receipt ID."""
        ctx = builder.ctx
        state = completion.authorize(uow, ctx, args["receipt_id"])
        stored, bound = state["receipt"], state["filing"]
        if stored["state"] == "applied":
            return
        application = {"outcome": stored["outcome"], "source_current": False, "document": None}
        if bound["source_kind"] == "document":
            row = uow.repository("documents", business_id=bound["business_id"]).get(bound["source_id"])
            if row is None or row.get("archived") or row["kind"] not in DOCUMENT_KINDS:
                application["reason"] = "source_missing"
            else:
                current = completion.source_current(uow, bound, self.current_digest(uow, row))
                application["source_current"] = current
                if current:
                    application["document"] = self._settle_document(builder, uow, row, bound, stored)
                else:
                    application["reason"] = "superseded"
        else:
            application["source_current"] = completion.source_current(uow, bound, bound["source_digest"])
        completion.applied_marker(builder, uow, stored, application)

    def _settle_document(self, builder, uow, row, bound, stored) -> str:
        """The matching current source is marked processed (published), failed with the reason (discarded) or retracted."""
        metadata = {key: value for key, value in row["metadata_json"].items() if key not in {"error", "last_attempt"}}
        marker = {"filing_id": bound["id"], "source_revision": bound["source_revision"], "outcome": stored["outcome"],
                  "completed_at": builder.at}
        if stored["outcome"] == "published":
            status = "retracted" if bound["operation"] == "remove" else "processed"
            metadata.update(status=status, processed=builder.at)
            marker.update(context_event_id=stored["context_event_id"], published_commit=stored["published_commit"])
        else:
            saved = stored["reason_json"] or {}
            reason = saved.get("reason") or (saved.get("request_evidence") or {}).get("reason") or saved.get("context") or "discarded"
            status = "failed"
            metadata.update(status=status, error=("Context input discarded: " + str(reason))[:ERROR_LIMIT], last_attempt=builder.at,
                            attempts=int(metadata.get("attempts", 0)) + 1)
        metadata["context_filing"] = marker
        self.store.write(builder, uow, kind=row["kind"], before=row, metadata=metadata, expected_revision=row["revision"])
        return status
