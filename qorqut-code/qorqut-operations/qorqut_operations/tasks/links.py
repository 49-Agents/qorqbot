"""Scoped Task associations. Store once on the source Task; read from either end.

This is revisioned Task metadata, not a second relationship authority. It neither
changes ownership/parents nor inherits state, permissions or subscriptions.
"""
from dataclasses import replace

from qorqut_operations.application.plans import IDENTITY, UUID, page, ref, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.types import attributes

KEY = "$task_links"
REFERENCE = schema({"task_id": IDENTITY, "business_id": {"anyOf": [UUID, {"type": "null"}]}}, ("task_id", "business_id"))
EDIT = schema({**REFERENCE["properties"], "linked": {"type": "boolean"}}, ("task_id", "business_id", "linked"))
HIDDEN = {"forbidden", "policy_denied", "not_found", "invalid_principal", "operation_unavailable"}


def stored(task):
    return task.get("fields_json", {}).get(KEY, [])


def identity(value):
    return value["business_id"], value["task_id"]


def resolve(operations, ctx, target, uow, builder=None):
    scope, task_id = identity(target)
    app = operations.application
    selected = app.current_context(replace(ctx, business_id=scope, review_business_id=scope), uow)
    descriptor = app.registry.get("tasks_get")
    arguments = {"task_id": task_id, "scope": "business" if scope else "global"}
    namespace = app.namespace(selected, descriptor, arguments)
    resource = ref("tasks", task_id, scope, namespace)
    app.authorize(selected, descriptor, (resource,), uow, namespace=namespace, arguments=arguments)
    row = uow.repository("tasks", business_id=scope).get(task_id, required=True)
    if builder:
        builder.expect(resource, row)
    return row


def edit(operations, ctx, current, task_id, change, uow, builder):
    target = {key: change[key] for key in ("business_id", "task_id")}
    if identity(target) == (ctx.business_id, task_id):
        raise DomainError("validation_error", "A Task cannot link to itself")
    other = resolve(operations, ctx, target, uow, builder)
    previous = list(stored(current))
    if change["linked"]:
        reverse = {"business_id": ctx.business_id, "task_id": task_id}
        if target not in previous and reverse not in stored(other):
            if len(previous) >= 100:
                raise DomainError("validation_error", "A Task can create up to 100 links")
            previous.append(target)
    else:
        # Incoming links are removed by updating the source Task shown in reads.
        previous = [item for item in previous if item != target]
    if previous:
        current["fields_json"][KEY] = previous
    else:
        current["fields_json"].pop(KEY, None)


def read(operations, ctx, task, uow, cursor=None):
    source = {"business_id": ctx.business_id, "task_id": task["id"]}
    candidates = [(target, source, task) for target in stored(task)]
    # SQLite selects only rows pointing to this exact scoped identity; unrelated
    # Task bodies are not materialized. Titles are resolved with current access.
    incoming = uow.connection.execute(
        "SELECT DISTINCT t.business_id,t.id FROM tasks t, json_each(t.fields_json, '$.\"$task_links\"') edge "
        "WHERE json_extract(edge.value,'$.business_id') IS ? AND json_extract(edge.value,'$.task_id')=?",
        (ctx.business_id, task["id"]))
    for row in incoming:
        target = {"business_id": row["business_id"], "task_id": row["id"]}
        candidates.append((target, target, None))
    found, seen = [], set()
    for target, origin, origin_row in candidates:
        key = identity(target)
        if key in seen:
            continue
        try:
            other = resolve(operations, ctx, target, uow)
        except DomainError as error:
            if error.code in HIDDEN:
                continue
            raise
        seen.add(key)
        business = uow.repository("businesses").get(key[0]) if key[0] else None
        found.append({**target, "id": other["id"], "title": other["title"], "status": other["status"],
                      "task_type": attributes(other)["task_type"], "archived": bool(other["archived"]),
                      "business_slug": business["slug"] if business else None,
                      "business_name": business.get("name", business["slug"]) if business else "Global",
                      "link_source": {**origin, "revision": (origin_row or other)["revision"]}})
    args = {"task_id": task["id"], **({"cursor": cursor} if cursor else {})}
    selected, next_cursor = page(found, args, ctx.business_id, "task_links", key=lambda item: (item["business_id"] or "", item["id"]), reverse=False)
    return {"related_tasks": selected, "related_tasks_next_cursor": next_cursor}
