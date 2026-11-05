"""A new resource's relationships to group Tasks, recorded in the same prepared plan that creates it.

Notes, uploads, assets, tables and the Context filer can relate what they create to one or more existing Tasks
(``task_ids``): each is an ordinary ``concerns`` relationship from the new item to the Task, so one resource can
belong to several groups and a Task can group any number of resources. A relationship is a label, not a grant: the Task
must exist in the caller's scope, the item's own domain already judged who may create it, and the edge is judged as a
``relationships_create`` by policy. Hierarchy is the Task's ``parent_id`` and is never inferred from these links.

``source_item`` names the shared item of an ordinary Operations document or resource. A host whose sources can be
other records passes its own ``item_resolver`` of the same shape; the injected resolver changes only how a stored
resource is named, never what the caller may read or relate."""

from qorqut_operations.application.plans import UUID, ref
from qorqut_operations.contracts import DomainError
from qorqut_operations.relationships import create_edge, endpoints, related_rows

from .vocabulary import RESOURCE_TYPES, document_type

TASK_IDS = {"type": "array", "maxItems": 200, "items": UUID, "uniqueItems": True,
            "description": "Existing Tasks in this project to relate the new item to (each a group Task or any related Task)."}
OPERATION = "relationships_create"


def source_document(ctx, uow, identity, builder=None):
    """The stored document of ``identity`` in the caller's scope, or the one the plan being built inserts."""
    document = uow.repository("documents", business_id=ctx.business_id).get(identity)
    if document is None and builder:
        document = next((change.values for change in builder.changes if change.resource.resource_type == "documents"
                         and change.resource.resource_id == identity and change.action == "insert"), None)
    if document is None:
        raise DomainError("not_found", "Document not found")
    return document


def source_item(ctx, uow, kind, identity, builder=None):
    """The shared item an authorized resource of ``kind`` ("documents" or a resource table) is known as."""
    if kind == "documents":
        return {"type": document_type(source_document(ctx, uow, identity, builder)), "id": identity}
    return {"type": RESOURCE_TYPES[kind], "id": identity}


def link_new_resource(builder, uow, kind, identity, task_ids=(), *, inherit_from=None, item_resolver=None):
    """Relate the new resource to ``task_ids`` (and, for a note made from an upload, to the Tasks the upload concerns).

    ``item_resolver(ctx, uow, kind, identity, builder=None)`` names the shared items; it defaults to ``source_item``."""
    ctx = builder.ctx
    wanted = set(task_ids)
    if not wanted and not inherit_from:
        return
    item_resolver = item_resolver or source_item
    item = item_resolver(ctx, uow, kind, identity, builder)
    if inherit_from:
        origin = item_resolver(ctx, uow, "documents", inherit_from)
        for edge in related_rows(uow, ctx.business_id, source=origin) + related_rows(uow, ctx.business_id, target=origin):
            if edge["role"] != "concerns":
                continue
            other = next(endpoint for endpoint in endpoints(edge) if endpoint != origin)
            if other["type"] == "task":
                builder.expect(ref("relationships", edge["id"], ctx.business_id), edge)
                wanted.add(other["id"])
    for task_id in sorted(wanted):
        task = uow.repository("tasks", business_id=ctx.business_id).get(task_id)
        if task is None:
            raise DomainError("not_found", "Task does not exist in this scope")
        builder.expect(ref("tasks", task_id, ctx.business_id), task)
        create_edge(builder, item, {"type": "task", "id": task_id}, "concerns", operation_name=OPERATION)


__all__ = ["OPERATION", "TASK_IDS", "link_new_resource", "source_document", "source_item"]
