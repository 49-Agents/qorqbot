"""The host's related-resource resolver: authorized reads of the retained domains' resources by table.

This is the shared resource lookup that deployed v1 kept inside ``StoryOperations.related``. It is constructed from the
actual ``Application`` and document service and nothing else, so item lookups, Tasks, campaigns, documents and Focus all
read through it without any of them constructing another domain's service. Every read keeps the domain's own checks: document
availability (``allow_busy`` then reads the last committed metadata only), the email and Signoff domains' read
handlers, the canonical read-only global Action reference, the active supported global's reach (never an agent
membership), and the resource's own read operation policy. Resolving a resource grants nothing the caller's policy would
not."""

from pathlib import PurePosixPath

from qorqut_operations.application.plans import ref
from qorqut_operations.contracts import DomainError, InvocationOptions
from qorqut_operations.items import authorize_item_read
from qorqut_operations.tasks.types import attributes as task_attributes

from bazarlyk.agents.global_scope import active_global, reaches_business

from ..activity import linked_action

DOCUMENT_TYPES = {"notes": {"business_note", "agent_note", "journal", "business_owned_note"},
                  "uploads": {"context_upload", "context_dump", "asset", "data_upload"}}
# The read operation that authorizes each resource table (documents are chosen by their stored kind below).
READ_OPERATIONS = {"tasks": "tasks_get", "action_logs": "actions_get", "agent_profiles": "agents_get",
                   "documents": "context_get", "signoffs": "signoffs_get", "email_conversations": "email_conversation_get",
                   "email_messages": "email_attachments_list", "data_tables": "tables_get", "products": "products_get"}
DOCUMENT_READS = {"business_note": "notes_get", "agent_note": "notes_get", "journal": "notes_get",
                  "business_owned_note": "notes_get", "plan": "plans_get", "content": "content_get",
                  "asset": "assets_get", "data_upload": "data_uploads_get"}


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
    def __init__(self, application, documents):
        if application is None or documents is None:
            raise ValueError("Related resources need the Application and the document service")
        self.application, self.documents = application, documents

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
        elif kind in {"email_conversations", "email_messages"}:
            row = uow.repository(kind, business_id=ctx.business_id).get(identity, required=True)
            operation = "email_conversation_get" if kind == "email_conversations" else "email_attachments_list"
            argument = "conversation_id" if kind == "email_conversations" else "message_id"
            # Reuse email ownership/plugin checks and deleted-message handling.
            arguments = {argument: identity, "limit": 1} if kind == "email_conversations" else {argument: identity}
            if ctx.business_id is None: arguments["scope"] = "global"  # Ordubashy's Global mailbox rows.
            self.application.registry.get(operation).read(ctx, arguments, InvocationOptions(), uow)
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
        elif kind == "agent_profiles":
            # An agent is an active supported global, read without a home: in a selected business it needs only its
            # derived reach to that active business. Its principal and profile are pinned, never a membership.
            row = uow.repository(kind, business_id=None).get(identity, required=True)
            principal = uow.repository("principals").get(identity)
            if active_global(uow.connection, identity) is None or (
                    ctx.business_id is not None and not reaches_business(uow.connection, identity, ctx.business_id)):
                raise DomainError("invalid_assignee", "Assign an active agent in this project")
            if builder:
                builder.expect(ref("principals", identity, None), principal)
                builder.expect(ref(kind, identity, None, "ecosystem" if ctx.business_id else None), row)
        else:
            row = uow.repository(kind, business_id=ctx.business_id).get(identity, required=True)
        if builder and kind != "agent_profiles":
            if kind == "action_logs":
                # The action's true storage identity; a Global-stored action is a read-only Global reference.
                builder.expect(ref(kind, row["id"], row["business_id"], None if row["business_id"] else "global"), row)
            else:
                builder.expect(ref(kind, identity, ctx.business_id), row)
        metadata = row.get("metadata_json", {})
        operation = READ_OPERATIONS[kind]
        if kind == "documents":
            operation = DOCUMENT_READS.get(row["kind"], "context_get")
        authorize_item_read(self.application, ctx, operation, uow)
        if kind == "data_tables":
            self.application.registry.get(operation).read(ctx, {"table_id": identity}, InvocationOptions(), uow)
        if kind == "signoffs":
            return {"label": view["title"], "state": view["state"], "kind": "signoff", "revision": view["status_revision"]}
        if kind == "email_messages":
            message = row["message_json"]
            return {"label": " · ".join(filter(None, [message.get("subject") or "Email message", message.get("from", {}).get("address"), message.get("sent_at") or message.get("received_at")])),
                    "kind": "email_message", "conversation_id": row["conversation_id"], "revision": row["revision"]}
        fallback = PurePosixPath(row["path"]).stem.replace("-", " ").replace("_", " ").title() if row.get("path") else identity
        label = (row.get("title") or row.get("summary") or row.get("subject") or metadata.get("title")
                 or metadata.get("name") or metadata.get("filename") or row.get("payload_json", {}).get("subject") or row.get("payload_json", {}).get("title")
                 or row.get("profile_json", {}).get("display_name") or row.get("name")
                 or fallback)
        return {**(_task_projection(row) if kind == "tasks" else {}),
                "label": label, "state": row.get("status", row.get("state", metadata.get("status"))), "path": row.get("path"),
                "kind": row.get("kind"), "revision": row.get("revision")}
