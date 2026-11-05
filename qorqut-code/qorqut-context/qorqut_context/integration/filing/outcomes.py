"""The ``context_filing`` implementation of the queue's terminal-outcome contribution.

``FilingOutcomes.record(uow, input_row)`` is called by the queue in the transaction that makes an input ``published`` or
``discarded`` (the driver's transitions and an unstarted-input discard control). For an input a filing bound it inserts the
filing's one stable completion receipt, with its pending application state; for any other input it records nothing.

The receipt ID is derived from the filing, so a repeated identical outcome returns the same row and a different outcome for
the same filing is refused. The outcome digest binds the filing, job, input, source version and its digest, the Context
event and published commit, or the exact discard reason; nothing is read from a caller. The input's stored payload is
re-checked against the version's digest first, so a receipt can never describe bytes other than the accepted ones.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid5

from qorqut_context.integration.filing import envelope
from qorqut_context.integration.filing.sources import require_system
from qorqut_operations.contracts import DomainError, canonical_hash, utc_now

RECEIPT_NAMESPACE = UUID("5c5c1f4e-6d0b-4a53-9c7a-2f0f3a1e9b10")
COLUMNS = ("id", "business_id", "filing_id", "job_id", "input_id", "source_version_id", "outcome", "context_event_id",
           "published_commit", "reason_json", "outcome_digest", "state", "application_json", "revision", "created_at",
           "resolved_at")


def receipt_id(filing_id: str) -> str:
    return str(uuid5(RECEIPT_NAMESPACE, "context-completion:" + filing_id))


def decode(row) -> dict | None:
    """A completion row as a plain dict with its JSON columns decoded."""
    if row is None:
        return None
    result = {name: row[name] for name in COLUMNS}
    for name in ("reason_json", "application_json"):
        result[name] = None if result[name] is None else json.loads(result[name])
    return result


def _bound(uow, input_id):
    return uow.connection.execute(
        "SELECT f.id AS filing_id, f.job_id, f.business_id, f.source_version_id, v.source_digest, v.source_path, v.operation, "
        "v.source_revision FROM context_filings f JOIN context_source_versions v ON v.id = f.source_version_id "
        "WHERE f.input_id=?", (input_id,)).fetchone()


def _verify_payload(row, bound) -> None:
    payload = json.loads(row["payload_json"])
    if payload.get("path") != bound["source_path"] or row["operation"] != bound["operation"] \
            or row["business_id"] != bound["business_id"]:
        raise DomainError("completion_conflict", "The input no longer matches its accepted source version")
    if bound["operation"] != "remove" and envelope.fingerprint(payload.get("content") or "") != bound["source_digest"]:
        raise DomainError("completion_conflict", "The input's frozen source differs from its accepted source version")


class FilingOutcomes:
    """The required terminal-outcome contribution of the Context queue, implemented over this schema."""

    def record(self, uow, input_row) -> dict | None:
        uow.require_write()
        if input_row["state"] not in ("published", "discarded"):
            raise DomainError("invalid_transition", "Only a final Context input has a terminal outcome")
        bound = _bound(uow, input_row["id"])
        if bound is None:
            return None
        _verify_payload(input_row, bound)
        saved = None if input_row["result_json"] is None else json.loads(input_row["result_json"])
        if input_row["state"] == "published":
            if not isinstance(saved, dict) or not saved.get("published_commit") or not input_row["context_event_id"]:
                raise DomainError("completion_conflict", "A published input needs its verified event and commit")
            facts = {"context_event_id": input_row["context_event_id"], "published_commit": saved["published_commit"],
                     "reason": None}
        else:
            if not isinstance(saved, dict):
                raise DomainError("completion_conflict", "A discarded input needs its recorded discard reason")
            facts = {"context_event_id": input_row["context_event_id"], "published_commit": None, "reason": saved}
        digest = canonical_hash({"filing_id": bound["filing_id"], "job_id": bound["job_id"], "input_id": input_row["id"],
                                 "source_version_id": bound["source_version_id"], "source_digest": bound["source_digest"],
                                 "outcome": input_row["state"], **facts})
        identity = receipt_id(bound["filing_id"])
        existing = uow.connection.execute("SELECT * FROM context_completions WHERE filing_id=?", (bound["filing_id"],)).fetchone()
        if existing is not None:
            if existing["outcome_digest"] != digest:
                raise DomainError("completion_conflict", "A different terminal outcome was already recorded for this filing")
            return decode(existing)
        uow.connection.execute(
            "INSERT INTO context_completions(id,business_id,filing_id,job_id,input_id,source_version_id,outcome,context_event_id,"
            "published_commit,reason_json,outcome_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, bound["business_id"], bound["filing_id"], bound["job_id"], input_row["id"], bound["source_version_id"],
             input_row["state"], facts["context_event_id"], facts["published_commit"],
             None if facts["reason"] is None else json.dumps(facts["reason"], sort_keys=True), digest, utc_now()))
        return decode(uow.connection.execute("SELECT * FROM context_completions WHERE id=?", (identity,)).fetchone())


def missing(uow) -> list[dict]:
    """Final bound inputs with no completion receipt yet: the gaps mechanical recovery closes."""
    return [dict(row) for row in uow.connection.execute(
        "SELECT i.* FROM context_inputs i JOIN context_filings f ON f.input_id = i.id "
        "LEFT JOIN context_completions c ON c.input_id = i.id "
        "WHERE i.state IN ('published','discarded') AND c.id IS NULL ORDER BY i.sequence")]


def record_missing(uow, *, driver_id: str) -> list[dict]:
    """Mechanical recovery: record every missing receipt, atomically and idempotently, with no provider run."""
    require_system(uow, driver_id)
    outcomes = FilingOutcomes()
    return [outcomes.record(uow, row) for row in missing(uow)]
