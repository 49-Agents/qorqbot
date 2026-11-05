"""Source-ownership checks the backend Context driver makes before any Context work.

A queue input names its source: a GENERAL input a canonical Context path, an Action input the accepted Action's key. This
module binds that source to the input's operational scope the first time it is added and afterwards lets only the same
scope edit or remove it. The scope always comes from the stored rows: a GENERAL input's own namespace, and for an Action
input the namespace of its frozen ``action_revisions`` row. Nothing a public caller supplies (metadata, ``origin_ref``,
text, a document or job reference) is read as authority, and no registered operation writes the table.

These are internal calls like ``transitions``: ``driver_id`` must be an active registered ``system`` principal. The
check and the reservation are one compare-and-set inside the caller's transaction, so the driver makes them in the same
transaction that starts the attempt and a refusal leaves no half-started work. Reading the shared Context never
authorizes a write: a source with no binding is refused for every scope, not adopted by whoever asks first.
"""

from __future__ import annotations

import json

from qorqut_context.integration.filing.schema import SOURCE_KINDS
from qorqut_context.integration.queue.queue import source_path
from qorqut_operations.contracts import DomainError, utc_now

SCOPE_CONFLICT = "source_scope_conflict"
UNBOUND = "source_unbound"


def require_system(uow, driver_id, *, write: bool = True) -> None:
    if write:
        uow.require_write()
    if not isinstance(driver_id, str) or uow.connection.execute(
            "SELECT 1 FROM principals WHERE id=? AND kind='system' AND active=1", (driver_id,)).fetchone() is None:
        raise DomainError("forbidden", "Only the backend system principal may bind or check Context sources")


def _input(uow, input_id):
    row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (input_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", "Context input does not exist")
    return row


def identity(uow, row) -> dict:
    """The ``{kind, key, business_id, operation}`` of a stored queue input, derived from stored state only.

    A GENERAL input's key is its (re-validated) canonical path and its scope the input's namespace. An Action input's key
    and scope are those of the exact accepted revision it snapshots; it is refused if the two disagree."""
    if row["input_kind"] == "general":
        return {"kind": "general", "key": source_path(json.loads(row["payload_json"])["path"], controlled=True),
                "business_id": row["business_id"], "operation": row["operation"]}
    revision = uow.connection.execute(
        "SELECT action_key, business_id, operation FROM action_revisions WHERE action_key=? AND revision=?",
        (row["item_id"], row["revision"])).fetchone()
    if revision is None or revision["business_id"] != row["business_id"] or revision["operation"] != row["operation"]:
        raise DomainError("validation_error", "The Action input does not match its stored revision")
    return {"kind": "action", "key": revision["action_key"], "business_id": revision["business_id"],
            "operation": revision["operation"]}


def binding(uow, kind: str, key: str):
    """The stored binding of a source, or None. A plain read for diagnostics and tests; it grants nothing."""
    if kind not in SOURCE_KINDS:
        raise DomainError("validation_error", "Unknown Context source kind")
    row = uow.connection.execute("SELECT * FROM context_source_bindings WHERE source_kind=? AND source_key=?", (kind, key)).fetchone()
    return None if row is None else {name: row[name] for name in ("source_kind", "source_key", "business_id", "bound_by_input", "bound_at")}


def reserve_source(uow, *, driver_id: str, input_id: str, exists: bool) -> dict:
    """Bind or verify the input's source before any Context work.

    ``exists`` is whether the source currently exists in the published Context (the driver reads that outside any
    transaction): a path for a GENERAL input, the Action's source identity for an Action input.

    * A bound source is available only to its own scope: any other scope is refused (``source_scope_conflict``).
    * An unbound source is first bound by an ``add`` of a source that does not yet exist. Any other case fails closed
      (``source_unbound``): an edit or removal of a source nobody owns, and an add over an existing source nobody owns.
      An edit or removal is never reinterpreted as an add.

    Returns ``{kind, key, business_id, binding: "reserved" | "verified"}``."""
    require_system(uow, driver_id)
    row = _input(uow, input_id)
    source = identity(uow, row)
    bound = binding(uow, source["kind"], source["key"])
    result = {"kind": source["kind"], "key": source["key"], "business_id": source["business_id"]}
    if bound is not None:
        if bound["business_id"] != source["business_id"]:
            raise DomainError(SCOPE_CONFLICT, "This Context source belongs to another scope and cannot be changed from this one")
        return {**result, "binding": "verified"}
    if source["operation"] != "add":
        raise DomainError(UNBOUND, f"This Context source has no recorded owner, so it cannot be {source['operation']}ed")
    if exists:
        raise DomainError(UNBOUND, "A Context source already exists here with no recorded owner, so it cannot be added over")
    uow.connection.execute(
        "INSERT INTO context_source_bindings(source_kind,source_key,business_id,bound_by_input,bound_at) VALUES (?,?,?,?,?)",
        (source["kind"], source["key"], source["business_id"], input_id, utc_now()))
    return {**result, "binding": "reserved"}
