"""Admission of one source version, its filing and its queue input, as part of the adapter's own prepared operation.

A filing adapter (the host's document and instruction operations) calls ``admit`` from ``prepare``, after it prepared the
registered job for this exact filing in the same builder. Nothing is written here: the version, the filing and the
queue input are three prepared records of the adapter's plan, applied in order in one transaction, so a source can never
exist without its job and queue entry, nor the reverse. The authority is the plan itself: ``admit`` finds the prepared job,
reads its program, definition and worker from it, and requires it to be the job built for this filing (its arguments are
exactly ``{"filing_id": ...}``). A caller-supplied path, filing ID, job ID or ``system`` label grants nothing, and the
database refuses a controlled adapter path or a filing that does not match such a job.

The lineage is authoritative and read from the accepted versions only, never from a later Context lookup: the first
version of a source is an ``add``, a later content version an ``edit`` (even after an earlier add was discarded: that edit
may block rather than silently become an add), a retraction a ``remove`` of one exact published version.
"""

from __future__ import annotations

from dataclasses import dataclass

from qorqut_context.integration.filing import envelope
from qorqut_context.integration.filing.schema import FILING_KINDS, PROGRAMS
from qorqut_context.integration.queue.queue import general_arguments, general_values
from qorqut_operations.contracts import DomainError, new_id, plain

# Context event metadata the filing sets for its input (the donor's accepted source-addition fields).
EVENT_METADATA = ("kind", "title", "origin", "origin_ref", "created_at")


@dataclass(frozen=True)
class Admission:
    version_id: str
    filing_id: str
    input_id: str
    source_revision: int
    operation: str
    path: str
    digest: str


def source_path(kind: str, source_id: str) -> str:
    if kind not in FILING_KINDS:
        raise DomainError("validation_error", "Unknown Context filing source kind")
    return FILING_KINDS[kind] + source_id + ".txt"


def new_filing_id() -> str:
    """The filing identity the adapter puts in its job's arguments before it prepares that job."""
    return new_id()


def versions(uow, kind: str, source_id: str) -> list[dict]:
    """The accepted versions of one source in revision order, each with its filing and queue input state (if any)."""
    return [dict(row) for row in uow.connection.execute(
        "SELECT v.*, f.id AS filing_id, f.input_id, f.job_id, i.state AS input_state FROM context_source_versions v "
        "LEFT JOIN context_filings f ON f.source_version_id = v.id LEFT JOIN context_inputs i ON i.id = f.input_id "
        "WHERE v.source_kind=? AND v.source_id=? ORDER BY v.source_revision", (kind, source_id))]


def lineage_operation(accepted: list[dict], retract: bool) -> str:
    """The operation the next version has: from the accepted lineage only."""
    if retract:
        return "remove"
    if not accepted or accepted[-1]["operation"] == "remove":
        return "add"
    return "edit"


def _prepared_job(builder, job_id: str, filing_id: str) -> dict:
    for change in builder.changes:
        if change.resource.resource_type == "jobs" and change.resource.resource_id == job_id and change.action == "insert":
            values = plain(change.values)
            break
    else:
        raise DomainError("invalid_plan", "A filing binds a registered job prepared in the same operation")
    if values.get("kind") not in PROGRAMS or plain(values.get("arguments_json")) != {"filing_id": filing_id}:
        raise DomainError("invalid_plan", "The prepared job is not the registered program for this filing")
    definition = plain(values["definition_ref_json"])
    return {"program_id": values["kind"], "program_version": definition["version"], "definition_digest": definition["digest"],
            "worker_id": values["principal_id"], "initiated_by": values.get("initiated_by_principal_id")}


def admit(builder, uow, *, kind: str, source_id: str, filing_id: str, job_id: str, document: str | None,
          event_metadata: dict, retracts: int | None = None, max_content_bytes: int | None = None,
          business_id: str | None) -> Admission:
    """Prepare the three records of one new source version and return its identities.

    ``document`` is the frozen ``qorqut-source-v1`` text (``envelope.source_document``), or ``None`` for a retraction
    (which names ``retracts``, the revision of the exact published version it removes). ``event_metadata`` is the Context
    source metadata (``kind``, ``title``, ``origin``, optional ``origin_ref``/``created_at``). ``business_id`` is the
    source's owning scope and must be the caller's selected scope."""
    ctx = builder.ctx
    if business_id != ctx.business_id:
        raise DomainError("forbidden", "A source is filed in the caller's selected scope")
    if kind == "instruction" and source_id != job_id:
        raise DomainError("invalid_plan", "An instruction's source is its own registered job")
    path = source_path(kind, source_id)
    accepted = versions(uow, kind, source_id)
    if accepted and accepted[0]["business_id"] != business_id:
        raise DomainError("forbidden", "This source belongs to another scope")
    operation = lineage_operation(accepted, retracts is not None)
    if operation == "remove":
        target = next((item for item in accepted if item["source_revision"] == retracts), None)
        latest_published = next((item for item in reversed(accepted) if item["input_state"] == "published"), None)
        if (document is not None or target is None or target["operation"] == "remove" or target["input_state"] != "published"
                or latest_published is not target):
            raise DomainError("invalid_transition", "Only the exact published version of a source can be retracted; "
                              "a pending input is discarded with its own control")
        if any(item["source_revision"] > target["source_revision"] and item["input_state"] not in ("published", "discarded")
               for item in accepted):
            raise DomainError("invalid_transition", "A newer version of this source is still pending; finish or discard it first")
        digest, content = target["source_digest"], None
    else:
        if document is None or retracts is not None:
            raise DomainError("validation_error", "A new source version supplies its frozen source document")
        envelope.parse(document)
        envelope.check_size(document, max_content_bytes)
        digest, content = envelope.fingerprint(document), document
        if accepted and accepted[-1]["source_digest"] == digest and accepted[-1]["operation"] != "remove" \
                and accepted[-1]["input_state"] != "discarded":
            raise DomainError("conflict", "This exact source version is already accepted; use its existing filing",
                              details={"filing_id": accepted[-1]["filing_id"], "source_revision": accepted[-1]["source_revision"]})
    program = _prepared_job(builder, job_id, filing_id)
    unknown = set(event_metadata) - set(EVENT_METADATA)
    if unknown:
        raise DomainError("validation_error", "Unknown Context source metadata: " + ", ".join(sorted(unknown)))
    change = general_arguments(operation, path, content, event_metadata if operation != "remove" else None, max_content_bytes,
                               controlled=True)
    version_id, input_id = new_id(), new_id()
    revision = len(accepted) + 1
    builder.record("context_source_versions", version_id, None, {
        "id": version_id, "business_id": business_id, "source_kind": kind, "source_id": source_id, "source_revision": revision,
        "operation": operation, "source_path": path, "source_digest": digest, "retracts_revision": retracts,
        "created_by": ctx.principal_id}, business_id=business_id, label=f"{path} v{revision}")
    builder.record("context_filings", filing_id, None, {
        "id": filing_id, "business_id": business_id, "source_version_id": version_id, "request_id": ctx.request_id,
        "job_id": job_id, **program, "input_id": input_id}, business_id=business_id, label=f"Filing of {path} v{revision}")
    builder.record("context_inputs", input_id, None, {**general_values(input_id, change, ctx.principal_id, builder.at),
                   "business_id": business_id}, business_id=business_id, label=path)
    builder.effects.append(f"Queues source version {revision} of {path} for the shared Context; it is processed later by the "
                           "backend driver and the source is not filed until its authorized completion applies.")
    return Admission(version_id, filing_id, input_id, revision, operation, path, digest)
