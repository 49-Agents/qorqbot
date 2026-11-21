"""Scoped Task associations. Store once on the source Task; read from either end.

This is revisioned Task metadata, not a second relationship authority. It neither
changes ownership/parents nor inherits state, permissions or subscriptions.
"""
from dataclasses import replace

from qorqut_operations.application.plans import IDENTITY, UUID, page, ref, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.types import attributes

KEY = "$task_links"
REFERENCE = schema({"task_id": IDENTITY, "project_id": {"anyOf": [UUID, {"type": "null"}]}}, ("task_id", "project_id"))
EDIT = schema({**REFERENCE["properties"], "linked": {"type": "boolean"}}, ("task_id", "project_id", "linked"))
HIDDEN = {"forbidden", "policy_denied", "not_found", "invalid_principal", "operation_unavailable"}


def stored(task):
    return task.get("fields_json", {}).get(KEY, [])


def identity(value):
    return value["project_id"], value["task_id"]


def resolve(operations, ctx, target, uow, builder=None):
    scope, task_id = identity(target)
    app = operations.application
    selected = app.current_context(replace(ctx, project_id=scope, review_project_id=scope), uow)
    descriptor = app.registry.get("tasks_get")
    arguments = {"task_id": task_id, "scope": "project" if scope else "global"}
    namespace = app.namespace(selected, descriptor, arguments)
    resource = ref("tasks", task_id, scope, namespace)
    app.authorize(selected, descriptor, (resource,), uow, namespace=namespace, arguments=arguments)
    row = uow.repository("tasks", project_id=scope).get(task_id, required=True)
    if builder:
        builder.expect(resource, row)
    return row


def edit(operations, ctx, current, task_id, change, uow, builder):
    target = {key: change[key] for key in ("project_id", "task_id")}
    if identity(target) == (ctx.project_id, task_id):
        raise DomainError("validation_error", "A Task cannot link to itself")
    other = resolve(operations, ctx, target, uow, builder)
    previous = list(stored(current))
    if change["linked"]:
        reverse = {"project_id": ctx.project_id, "task_id": task_id}
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
    source = {"project_id": ctx.project_id, "task_id": task["id"]}
    candidates = [(target, source, task) for target in stored(task)]
    # SQLite selects only rows pointing to this exact scoped identity; unrelated
    # Task bodies are not materialized. Titles are resolved with current access.
    incoming = uow.connection.execute(
        "SELECT DISTINCT t.project_id,t.id FROM tasks t, json_each(t.fields_json, '$.\"$task_links\"') edge "
        "WHERE json_extract(edge.value,'$.project_id') IS ? AND json_extract(edge.value,'$.task_id')=?",
        (ctx.project_id, task["id"]))
    for row in incoming:
        target = {"project_id": row["project_id"], "task_id": row["id"]}
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
        project = uow.repository("projects").get(key[0]) if key[0] else None
        shown = attributes(other)
        found.append({**target, "id": other["id"], "title": other["title"], "status": other["status"],
                      "task_type": shown["task_type"], "customer_kind": shown["customer_kind"],
                      "customer_state": shown["customer_state"], "archived": bool(other["archived"]),
                      "project_slug": project["slug"] if project else None,
                      "project_name": project.get("name", project["slug"]) if project else "Global",
                      "link_source": {**origin, "revision": (origin_row or other)["revision"]}})
    args = {"task_id": task["id"], **({"cursor": cursor} if cursor else {})}
    selected, next_cursor = page(found, args, ctx.project_id, "task_links", key=lambda item: (item["project_id"] or "", item["id"]), reverse=False)
    return {"related_tasks": selected, "related_tasks_next_cursor": next_cursor}
