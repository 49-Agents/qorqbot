"""Registered files: the verified final bytes of a file receipt as shared history, seeds and document lifecycle events.

The shared history library is written on the caller's unit-of-work connection in the finalising transaction, before the
receipt is marked applied."""

from __future__ import annotations

import base64
import hashlib
import json

from qorqut_operations.contracts import DomainError, plain, utc_now
from .text import EXTERNAL_AUTHOR, UNTRACKED_KINDS, append_event, latest, namespace, save_text


def _plan_previews(plan):
    preview = plain(plan.preview)
    result = {}
    for item in preview.get("changes", ()):
        if item.get("kind") == "text" and item["resource"]["resource_type"] == "documents":
            result.setdefault(item["resource"]["resource_id"], []).append(item["before"])
    return result


def proven_before(previews, document_id, before_hash):
    """Pre-edit text frozen in the plan preview, only when it hashes to the verified before bytes."""
    if not before_hash:
        return None
    for text in previews.get(document_id, ()):
        if hashlib.sha256(text.encode("utf-8")).hexdigest() == before_hash.removeprefix("sha256:"):
            return text
    return None


def file_effects(plan):
    """Final written bytes and before hashes of managed document files, per document."""
    writes, deleted = {}, {}
    for change in plan.changes:
        if change.kind != "file" or change.resource.resource_type != "managed_document_files":
            continue
        document_id = json.loads(change.resource.resource_id)["document_id"]
        if change.action == "delete":
            deleted[document_id] = change.values["before_hash"]
        elif change.values.get("content_b64") is not None:
            writes[document_id] = (base64.b64decode(change.values["content_b64"], validate=True), change.values["before_hash"])
    return writes, deleted


class DocumentReceiptHistory:
    """The receipt history of registered document files.

    ``byte_capture`` maps a document kind to the host callback that records that kind's committed bytes itself,
    ``callback(connection, row, text, receipt_id) -> version`` (a kind whose provenance lives outside this mechanism). It is a
    required, explicit argument: a host with no such kind passes an empty mapping, and one that forgets it cannot compose."""

    def __init__(self, *, byte_capture):
        captures = dict(byte_capture)
        if any(not isinstance(kind, str) or not callable(capture) for kind, capture in captures.items()):
            raise TypeError("A byte-capture callback is registered per document kind and must be callable")
        self.byte_capture = captures

    def capture_receipt(self, uow, receipt, plan):
        """Final verified file effects of an applied receipt, in its finalising transaction."""
        connection = uow.connection
        writes, deleted = file_effects(plan)
        previews = _plan_previews(plan) if writes else {}
        pending = getattr(uow, "history_events", {})
        for document_id, (data, before_hash) in writes.items():
            row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if row is None or row["kind"] in UNTRACKED_KINDS:
                pending.pop(document_id, None)
                continue
            if hashlib.sha256(data).hexdigest() != row["content_hash"].removeprefix("sha256:"):
                raise DomainError("apply_incomplete", "Committed document does not match its verified file effect")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                pending.pop(document_id, None)
                continue
            if row["kind"] in self.byte_capture:
                version = self.byte_capture[row["kind"]](connection, row, text, receipt["id"])
            else:
                before = proven_before(previews, document_id, before_hash or deleted.get(document_id))
                reason = f"{plan.operation_name}; documents revision {row['revision']}; receipt {receipt['id']}"
                if before is None and latest(connection, namespace(row["business_id"]), document_id) is None and before_hash:
                    reason += "; earlier content was not recorded, history starts here"
                version = save_text(connection, namespace(row["business_id"]), document_id, text, plan.principal_id, reason,
                    before=before, drift_author=EXTERNAL_AUTHOR,
                    drift_reason="Observed external content accepted by the next edit; author unknown")
            event = pending.pop(document_id, None)
            if event is not None:
                event["data"].update(history_version=version.version, receipt_id=receipt["id"], signoff_id=receipt["signoff_id"])
                append_event(connection, **event)
        for document_id in list(pending):
            # A record-only change of a document in this plan has no file effect to wait for.
            append_event(connection, **pending.pop(document_id))


def seed_document(connection, row, data, *, author, operation, plan=None):
    """Creation of a registered document outside DocumentStore (bootstrap, conversion).

    ``data`` must be trusted bytes already in the seed or plan; they are checked
    against the registered hash. Records version 1 and a creation event.
    """
    if row["kind"] in UNTRACKED_KINDS:
        return None
    if hashlib.sha256(data).hexdigest() != row["content_hash"].removeprefix("sha256:"):
        raise DomainError("document_conflict", "Seeded document bytes do not match the registered hash")
    version = save_text(connection, namespace(row["business_id"]), row["id"], data.decode("utf-8"), author,
                        f"{operation}; documents revision {row['revision']}; created")
    event = document_event(None, row, actor_id=author, operation=operation, plan=plan)
    if event is not None:
        event["data"]["history_version"] = version.version
        append_event(connection, **event)
    return version


def document_changed(connection, before, after, *, actor_id, operation):
    """Lifecycle event for a record-only documents change made by a trusted bypass writer."""
    event = document_event(before, after, actor_id=actor_id, operation=operation)
    return append_event(connection, **event) if event is not None else None


def document_event(row_before, row_after, *, actor_id, operation, plan=None):
    """The lifecycle event for a committed documents row change, or None."""
    row = row_after or row_before
    if row["kind"] in UNTRACKED_KINDS or not row.get("business_id"):
        return None
    if row_after is None:
        kind, revision, occurred = "document.deleted", row_before["revision"], utc_now()
    elif row_before is None:
        kind, revision, occurred = "document.created", row_after["revision"], row_after["updated_at"]
    elif row_after["revision"] == row_before["revision"]:
        return None
    else:
        revision, occurred = row_after["revision"], row_after["updated_at"]
        if bool(row_before.get("archived")) != bool(row_after.get("archived")):
            kind = "document.archived" if row_after.get("archived") else "document.unarchived"
        elif row_before["path"] != row_after["path"]:
            kind = "document.moved"
        else:
            kind = "document.updated"
    data = {"document_id": row["id"], "document_kind": row["kind"], "revision": revision, "operation": operation}
    if row_before is not None and row_after is not None:
        data["content_changed"] = row_before["content_hash"] != row_after["content_hash"]
    if plan is not None:
        data.update(initiated_by_principal_id=plan.initiated_by_principal_id, proposer_agent_id=plan.proposer_agent_id)
    verb = kind.split(".", 1)[1]
    return {"business_id": row["business_id"], "event_id": f"document:{row['id']}:r{revision}:{verb}", "kind": kind,
            "actor_id": actor_id, "summary": f"Document {verb}", "subject_type": "document", "subject_id": row["id"],
            "occurred_at": occurred, "data": data}
