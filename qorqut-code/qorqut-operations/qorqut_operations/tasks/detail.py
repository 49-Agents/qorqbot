"""The full Task view, built for a whole page of Tasks in a fixed number of queries.

``tasks_list`` and ``tasks_get`` show the same view of a Task. Built one Task at a time it cost about ten queries per row
(creator, blockers and their states, the Tasks it blocks, parent, children, follow state, labels, comment count,
assignees, Workers); here each of those is one query for the page, so a page of fifty costs the same handful of reads as a
page of one. The view also names what the ids point at (``assignees``, ``blockers``, ``business_name``) so a reader does
not need a second lookup to say who or what they are."""

from __future__ import annotations

from qorqut_operations.discussion.follows import ThreadItem
from qorqut_operations.tasks import lifecycle
from qorqut_operations.tasks.models import snooze_view
from qorqut_operations.tasks.schema import CLOSED_STATES, PAUSED_STATES
from qorqut_operations.tasks.types import attributes, custom_fields
from qorqut_operations.tasks.workers import People, project_workers

HIDDEN = frozenset({"fields_json", "worker_ids_json", "snoozed_until", "snooze_generation"})
CHUNK = 500


def chunks(values):
    values = list(dict.fromkeys(values))
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def marks(values) -> str:
    return ",".join("?" * len(values))


class Lookup(People):
    """Rows read once per page, however many Tasks name them."""

    def __init__(self, uow):
        super().__init__(uow)
        self.businesses = {}

    def business_name(self, business_id):
        if business_id is None:
            return "Global"
        if business_id not in self.businesses:
            row = self.uow.repository("businesses").get(business_id)
            self.businesses[business_id] = (row.get("name") or row.get("slug")) if row else None
        return self.businesses[business_id]

    def person(self, identity):
        principal = self.principal(identity)
        if not principal:
            return {"id": identity, "kind": None, "name": None}
        return {"id": identity, "kind": principal["kind"], "name": principal.get("display_name")}


def grouped(connection, sql, business_id, ids, *, extra=()):
    """``{key: [row, ...]}`` for one query run over ``ids`` in chunks; the query's first column is the key."""
    found = {}
    for part in chunks(ids):
        for row in connection.execute(sql.format(ids=marks(part)), (business_id, *extra, *part)):
            found.setdefault(row[0], []).append(row)
    return found


def scope_reads(uow, business_id, tasks, principal_id):
    """Everything the views of one scope's Tasks need, one query per kind of fact."""
    connection, ids = uow.connection, [task["id"] for task in tasks]
    blockers = grouped(connection,
        "SELECT DISTINCT from_id, to_id FROM relationships WHERE business_id IS ? AND from_type='task' AND from_id IN ({ids}) "
        "AND to_type='task' AND role='blocked_by' ORDER BY to_id", business_id, ids)
    blockers = {key: [row[1] for row in rows] for key, rows in blockers.items()}
    referenced = [*(blocker for found in blockers.values() for blocker in found), *(task["parent_id"] for task in tasks if task.get("parent_id"))]
    related = {}
    for part in chunks(referenced):
        for row in connection.execute(f"SELECT id,title,status FROM tasks WHERE business_id IS ? AND id IN ({marks(part)})",
                                      (business_id, *part)):
            related[row["id"]] = {"id": row["id"], "title": row["title"], "status": row["status"]}
    blocks = grouped(connection,
        "SELECT DISTINCT r.to_id, t.id FROM relationships r JOIN tasks t ON t.business_id IS r.business_id AND t.id=r.from_id "
        "WHERE r.business_id IS ? AND r.from_type='task' AND r.to_type='task' AND r.to_id IN ({ids}) AND r.role='blocked_by' "
        "AND t.status NOT IN ('done','cancelled') ORDER BY t.id", business_id, ids)
    children = grouped(connection,
        "SELECT parent_id, id, title, status FROM (SELECT parent_id, id, title, status, "
        "ROW_NUMBER() OVER (PARTITION BY parent_id ORDER BY created_at, id) AS position FROM tasks "
        "WHERE business_id IS ? AND parent_id IN ({ids})) WHERE position <= 51 ORDER BY parent_id, position", business_id, ids)
    follows = grouped(connection,
        "SELECT item_id, following, reason FROM thread_follows WHERE business_id IS ? AND item_type='task' AND principal_id=? "
        "AND item_id IN ({ids})", business_id, ids, extra=(principal_id,))
    labels = grouped(connection,
        "SELECT i.item_id, t.label FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.business_id IS ? AND i.item_type='task' "
        "AND i.item_id IN ({ids}) ORDER BY t.label_key", business_id, ids)
    counts = grouped(connection,
        "SELECT item_id, count(*) FROM thread_messages WHERE business_id IS ? AND item_type='task' AND kind='comment' "
        "AND item_id IN ({ids}) GROUP BY item_id", business_id, ids)
    assigned = grouped(connection,
        "SELECT from_id, to_id FROM relationships WHERE business_id IS ? AND from_type='task' AND role='assigned_to' "
        "AND from_id IN ({ids}) ORDER BY to_id", business_id, ids)
    return {"blockers": blockers, "related": related,
            "blocks": {key: [row[1] for row in rows] for key, rows in blocks.items()},
            "children": {key: [{"id": row[1], "title": row[2], "status": row[3]} for row in rows] for key, rows in children.items()},
            "follows": {key: rows[0] for key, rows in follows.items()},
            "labels": {key: [row[1] for row in rows] for key, rows in labels.items()},
            "counts": {key: rows[0][1] for key, rows in counts.items()},
            "assigned": {key: [row[1] for row in rows] for key, rows in assigned.items()}}


def task_views(ctx, tasks, uow, now, lookup=None):
    """The full view of each Task, in the order given, without comments (``tasks_get`` adds those)."""
    lookup = lookup or Lookup(uow)
    scopes = {}
    for task in tasks:
        scopes.setdefault(task["business_id"], []).append(task)
    reads = {business_id: scope_reads(uow, business_id, members, ctx.principal_id) for business_id, members in scopes.items()}
    views = []
    for task in tasks:
        business_id, task_id, read = task["business_id"], task["id"], reads[task["business_id"]]
        item = ThreadItem(business_id, "task", task_id)
        creator = lookup.principal(task.get("created_by")) if task.get("created_by") else None
        blockers = read["blockers"].get(task_id, [])
        found = [read["related"].get(blocker) for blocker in blockers]
        unresolved = [row["id"] for row in found if row is not None and row["status"] not in CLOSED_STATES]
        parent = read["related"].get(task["parent_id"]) if task.get("parent_id") else None
        children = read["children"].get(task_id, [])
        follow = read["follows"].get(task_id)
        assignee_ids = read["assigned"].get(task_id, [])
        views.append({
            **{key: value for key, value in task.items() if key not in HIDDEN},
            **snooze_view(task, now),
            "business_name": lookup.business_name(business_id),
            "created_by_name": creator.get("display_name", "") if creator else "",
            "created_by_kind": creator.get("kind", "") if creator else "",
            "tags": read["labels"].get(task_id, []), "fields": custom_fields(task), **attributes(task),
            "thread_id": item.thread_id, "note_count": read["counts"].get(task_id, 0),
            **project_workers(uow, task, lookup),
            "assignee_ids": assignee_ids, "assignees": [lookup.person(identity) for identity in assignee_ids],
            "archived": bool(task.get("archived")),
            "blocked_by": blockers, "unresolved_blockers": sorted(set(unresolved)), "blocks": read["blocks"].get(task_id, []),
            "blockers": [row if row is not None else {"id": blocker, "title": None, "status": None}
                         for blocker, row in zip(blockers, found)],
            "parent": dict(parent) if parent else None,
            "children": children[:50], "children_has_more": len(children) > 50,
            "closed": task["status"] in CLOSED_STATES, "paused": task["status"] in PAUSED_STATES,
            "stalled": lifecycle.stalled(task, now),
            "following": bool(follow and follow["following"]), "follow_reason": follow["reason"] if follow else None})
    return views
