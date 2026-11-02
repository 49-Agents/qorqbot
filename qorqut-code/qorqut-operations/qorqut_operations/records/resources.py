"""Authorized reads of the neutral records by table: the shared resource lookup that deployed v1 kept inside
``StoryOperations.related``.

It is constructed from the actual ``Application``, the document service and the host's ``ResourceHost``, so item lookups,
Tasks, documents and Focus all read through it without any of them constructing another domain's service. Every read keeps
the domain's own checks: document availability (``allow_busy`` then reads the last committed metadata only), the Signoff
domain's read handler, the canonical read-only global Action reference, and the resource's own read operation policy.
Resolving a resource grants nothing the caller's policy would not.

The host's own resource tables (and the agent profile, whose reach is the host's configured-global identity) are read by
``ResourceHost`` at one fixed point: right after the document case, before the generic table cases. ``read_special`` returns
the row it read or ``None`` (reading nothing) for any other table, which then falls through to the generic cases;
``project_special`` returns the compact view of a host row or ``None``."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Protocol

from qorqut_operations.actions.writeback import linked_action
from qorqut_operations.application.plans import ref
from qorqut_operations.contracts import DomainError, InvocationOptions
from qorqut_operations.items import authorize_item_read
from qorqut_operations.tasks.types import attributes as task_attributes

DOCUMENT_TYPES = {"notes": {"business_note", "agent_note", "journal", "business_owned_note"},
                  "uploads": {"context_upload", "context_dump", "asset", "data_upload"}}
# The read operation that authorizes each generic resource table (documents are chosen by their stored kind below); the
# host adds the read operations of its own tables to the map it passes in.
READ_OPERATIONS = {"tasks": "tasks_get", "action_logs": "actions_get", "agent_profiles": "agents_get",
                   "documents": "context_get", "signoffs": "signoffs_get", "data_tables": "tables_get"}
DOCUMENT_READS = {"business_note": "notes_get", "agent_note": "notes_get", "journal": "notes_get",
                  "business_owned_note": "notes_get", "plan": "plans_get", "content": "content_get",
                  "asset": "assets_get", "data_upload": "data_uploads_get"}


class ResourceHost(Protocol):
    """The host's resource tables and agent reach, called by ``RelatedResources.get``.

    ``read_special(ctx, uow, kind, identity, builder)`` reads, authorizes (with the domain's own handler) and pins what
    it owns and returns the stored row, or returns ``None`` without reading anything for a table that is not the host's.
    ``project_special(kind, row)`` is the compact view of a host row after the generic authorization, or ``None``."""

    def read_special(self, ctx, uow, kind: str, identity: str, builder) -> Mapping[str, Any] | None: ...

    def project_special(self, kind: str, row: Mapping[str, Any]) -> Mapping[str, Any] | None: ...


def resource_table(kind):
    return "documents" if kind in DOCUMENT_TYPES else kind


def _task_projection(row):
    """The compact Task type of a related item. A Customer also shows its kind and state label, nothing of its profile or notes."""
    attributes = task_attributes(row)
    if attributes["task_type"] != "customer":
        return {"task_type": attributes["task_type"]}
    return {"task_type": "customer", "customer_kind": attributes.get("customer_kind"),
            "customer_state": attributes.get("customer_state") or ""}


class RelatedResources:
    def __init__(self, application, documents, *, host, read_operations):
        if application is None or documents is None:
            raise ValueError("Related resources need the Application and the document service")
        if host is None or read_operations is None:
            raise ValueError("Related resources need the resource host and its read operations")
        self.application, self.documents, self.host, self.read_operations = application, documents, host, read_operations

    def get(self, ctx, uow, kind, identity, builder=None, *, allow_busy=False):
        requested_kind, kind = kind, resource_table(kind)
        if kind == "documents":
            try:
                row = self.documents.store.get(ctx, identity, uow)
            except DomainError as exc:
                if exc.code != "busy" or not allow_busy:
                    raise
                # Reading canonical links needs only the last committed metadata;
                # it never opens files still owned by an application receipt.
                row = uow.repository("documents", business_id=ctx.business_id).get(identity, required=True)
            if row["business_id"] != ctx.business_id:
                raise DomainError("not_found", "Choose a document in this project")
            if requested_kind in DOCUMENT_TYPES and row["kind"] not in DOCUMENT_TYPES[requested_kind]:
                raise DomainError("not_found", "Document is not a " + requested_kind + " resource")
        elif (row := self.host.read_special(ctx, uow, kind, identity, builder)) is not None:
            pass  # one of the host's own tables or an agent profile: read, authorized and pinned by the host
        elif kind == "signoffs":
            descriptor = self.application.registry.get("signoffs_get")
            # The signoff domain enforces review-business, global-capability and target visibility.
            view = descriptor.read(ctx, {"signoff_id": identity}, InvocationOptions(), uow)["signoff"]
            row = uow.repository(kind, business_id=ctx.business_id).get(identity, required=True)
        elif kind == "action_logs":
            # Canonical action_id or legacy bare ID, authorized by membership.
            row = linked_action(ctx, uow, identity)
            if row is None:
                raise DomainError("not_found", "Resource does not exist")
        else:
            row = uow.repository(kind, business_id=ctx.business_id).get(identity, required=True)
        if builder and kind != "agent_profiles":
            if kind == "action_logs":
                # The action's true storage identity; a Global-stored action is a read-only Global reference.
                builder.expect(ref(kind, row["id"], row["business_id"], None if row["business_id"] else "global"), row)
            else:
                builder.expect(ref(kind, identity, ctx.business_id), row)
        metadata = row.get("metadata_json", {})
        operation = self.read_operations[kind]
        if kind == "documents":
            operation = DOCUMENT_READS.get(row["kind"], "context_get")
        authorize_item_read(self.application, ctx, operation, uow)
        if kind == "data_tables":
            self.application.registry.get(operation).read(ctx, {"table_id": identity}, InvocationOptions(), uow)
        if kind == "signoffs":
            return {"label": view["title"], "state": view["state"], "kind": "signoff", "revision": view["status_revision"]}
        special = self.host.project_special(kind, row)
        if special is not None:
            return special
        fallback = PurePosixPath(row["path"]).stem.replace("-", " ").replace("_", " ").title() if row.get("path") else identity
        label = (row.get("title") or row.get("summary") or row.get("subject") or metadata.get("title")
                 or metadata.get("name") or metadata.get("filename") or row.get("payload_json", {}).get("subject") or row.get("payload_json", {}).get("title")
                 or row.get("profile_json", {}).get("display_name") or row.get("name")
                 or fallback)
        return {**(_task_projection(row) if kind == "tasks" else {}),
                "label": label, "state": row.get("status", row.get("state", metadata.get("status"))), "path": row.get("path"),
                "kind": row.get("kind"), "revision": row.get("revision")}
