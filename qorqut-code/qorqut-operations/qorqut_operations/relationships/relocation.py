"""Which relationships travel when whole records (Tables, project documents) move between projects.

A relationship row belongs to one scope. When records move, an edge whose every other end is moving with them, is an agent,
or already sits in the destination (a Task, Table or document moved there earlier: the edge reunites them) travels with the
move. Any other edge stays in the source scope as written, as history, and is still read from both ends. A travelling edge
with Context review decisions stays too, since those decisions cannot be rewritten; for a workflow role that would split a
live dependency, so it blocks instead."""

from __future__ import annotations

from qorqut_operations.storage.record_moves import moved_location as moved_record
from qorqut_operations.tasks.relocation import WORKFLOW_ROLES, blocker, existing_tables, marks, moved_location as moved_task

# The relationship end types each kind of moved record is addressed by.
DOCUMENT_ENDS = frozenset({"material", "person", "topic", "document"})


def current_place(uow, kind, scope, identity):
    """Where the record a stale ``(scope, kind, id)`` edge end names is now, or ``scope`` while it is still there."""
    if kind == "task":
        return moved_task(uow, scope, identity) or scope
    if kind == "table":
        return moved_record(uow, "table", scope, identity) or scope
    if kind in DOCUMENT_ENDS:
        return moved_record(uow, "document", scope, identity) or scope
    return scope


def classify(uow, source, destination, moving):
    """``(carried edges, historical count, blockers, material)`` for the edges touching ``moving`` (``{(type, id)}``)."""
    connection = uow.connection
    by_type = {}
    for kind, identity in moving:
        by_type.setdefault(kind, []).append(identity)
    edges, seen = [], set()
    for kind, ids in sorted(by_type.items()):
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            for edge in connection.execute("SELECT * FROM relationships WHERE project_id=? AND ((from_type=? AND from_id IN " + marks(chunk)
                                           + ") OR (to_type=? AND to_id IN " + marks(chunk) + ")) ORDER BY id",
                                           (source, kind, *chunk, kind, *chunk)):
                if edge["id"] not in seen:
                    seen.add(edge["id"])
                    edges.append(edge)
    edges.sort(key=lambda edge: edge["id"])
    reviewed = set()
    if edges and "context_reviews" in existing_tables(uow):
        keys = [edge["id"] for edge in edges]
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            reviewed.update(row[0] for row in connection.execute("SELECT DISTINCT relationship_id FROM context_reviews WHERE project_id=? "
                                                                 "AND relationship_id IN " + marks(chunk), (source, *chunk)))
    carried, historical, blockers = [], 0, []
    for edge in edges:
        ends = ((edge["from_type"], edge["from_id"]), (edge["to_type"], edge["to_id"]))
        others = [end for end in ends if end not in moving]
        travels = all(kind == "agent" or current_place(uow, kind, source, identity) == destination for kind, identity in others)
        if travels and edge["id"] not in reviewed:
            carried.append(edge)
        elif travels and edge["role"] in WORKFLOW_ROLES:
            blockers.append(blocker("reviewed_relationship", "A moved relationship has Context review decisions that cannot be rewritten"))
        else:
            historical += 1
    if carried and connection.execute("SELECT 1 FROM relationships WHERE project_id=? AND id IN " + marks([edge["id"] for edge in carried])
                                      + " LIMIT 1", (destination, *[edge["id"] for edge in carried])).fetchone():
        blockers.append(blocker("relationship_id_exists", "The destination project already has a relationship with the same id"))
    material = [[edge["id"], edge["revision"], edge["role"]] for edge in edges]
    return carried, historical, blockers, material


def carry(uow, source, destination, edges):
    for edge in edges:
        uow.connection.execute("UPDATE relationships SET project_id=? WHERE project_id=? AND id=?", (destination, source, edge["id"]))


__all__ = ["DOCUMENT_ENDS", "carry", "classify", "current_place"]
