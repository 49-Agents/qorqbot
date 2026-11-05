"""Read-only display of the typed references an immutable note holds, across Task relocations.

A note's references are stored as ``{type, id}`` relative to the scope the note was written in, and a stored note never
changes. A relocation moves the Task that holds the note, and may move the Task a note names, so the display - never the
payload - resolves each reference like this:

1. In the scope it was written in: ``reference_namespace`` for a note whose Task has moved (the first relocation pins it), else
   the reader's own scope. A record at that exact scope always wins, so a destination record that merely shares a bare id is
   never mistaken for the original target.
2. For a Task that is no longer there, the audited relocation resolver names where that very Task is now, and only a caller who
   is authorized to read it there sees its label and place. Anything else - no access, an ambiguous identity, a record that
   never existed - is shown as unavailable and discloses nothing.

A readable reference whose identity is scope-qualified (a Task, and the documents and tables a scope owns) always carries the
authorized place it resolved in - ``business_id`` (an explicit ``None`` is Global), ``business_slug`` and ``business_name`` - so a
client opens exactly that record and never has to reconstruct the scope. Any other reference carries them only when it resolved
outside the reader's own scope. An unavailable reference carries none. Writes and strict validation never use this path: they
resolve in the caller's current scope and a moved target is refused there."""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks import links
from qorqut_operations.tasks.relocation import moved_location, scope_of

HIDDEN = {"not_found", "forbidden", "policy_denied", "operation_unavailable"}
# Kinds whose identity is ``(scope, id)``: a readable reference of these always names the scope it resolved in.
SCOPED = frozenset({"task", "topic", "material", "table"})
# No Task is there for the reader to open.
ABSENT = object()


def stored_namespace(row):
    """The ``reference_namespace`` of a stored message (a dict or a row), or ``None`` when it has never needed one."""
    return row["reference_namespace"] if "reference_namespace" in row.keys() else None


class HistoricalReferences:
    """The display resolver of one ``TaskOperations``: its item resolver, host reference types and application."""

    def __init__(self, operations):
        self.operations = operations

    def show(self, ctx, refs, uow, namespace=None):
        """``refs`` as the reader may see them. ``namespace`` is the message's stored ``reference_namespace`` (or ``None``)."""
        if namespace is None:
            return [self.one(ctx, item, uow, ctx.business_id) for item in refs]
        valid, origin = scope_of(namespace)
        return [self.one(ctx, item, uow, origin) if valid else {"unavailable": True} for item in refs]

    def one(self, ctx, item, uow, origin):
        try:
            return self.resolved(ctx, item, uow, origin)
        except DomainError as error:
            if error.code not in HIDDEN:
                raise
        if item["type"] == "task":
            location = moved_location(uow, origin, item["id"])
            if location is not None:
                try:
                    # The exact current target is authorized (its own read operation, overrides included) before it is shown.
                    links.resolve(self.operations, ctx, {"business_id": location, "task_id": item["id"]}, uow)
                    return self.resolved(ctx, item, uow, location)
                except DomainError:
                    pass
        return {"unavailable": True}

    def resolved(self, ctx, item, uow, scope):
        scoped = self.context(ctx, scope, uow)
        references = self.operations.ports.references
        view = references.resolve(scoped, item, uow) if item["type"] in references.host_types else self.operations.items.resolve(scoped, item, uow)
        shown = {**item, "label": view["label"], "state": view.get("state"),
                 **({"resource_type": view["resource_type"]} if item["type"] == "material" else {})}
        if item["type"] not in references.host_types and (item["type"] in SCOPED or scope != ctx.business_id):
            shown.update(self.place(uow, scope))
        return shown

    def target(self, ctx, task_id, uow, origin):
        """The scope a Task reference written in ``origin`` names for this reader, for consumers that open the Task themselves
        (CRM note-derived discovery): ``origin`` while a Task with that exact identity is still there - whoever the reader is,
        so a same-id Task elsewhere is never substituted - else the place the audited resolver names, but only when the reader
        may read the Task there. ``ABSENT`` for anything else; no location is disclosed."""
        if uow.connection.execute("SELECT 1 FROM tasks WHERE business_id IS ? AND id=?", (origin, task_id)).fetchone():
            return origin
        location = moved_location(uow, origin, task_id)
        if location is None:
            return ABSENT
        try:
            links.resolve(self.operations, ctx, {"business_id": location, "task_id": task_id}, uow)
        except DomainError:
            return ABSENT
        return location

    def context(self, ctx, scope, uow):
        """``ctx`` selected on ``scope`` with the caller's current reach there; any failure reads as an absent reference."""
        if scope == ctx.business_id:
            return ctx
        try:
            return self.operations.application.current_context(replace(ctx, business_id=scope, review_business_id=scope), uow)
        except DomainError:
            raise DomainError("not_found", "Reference scope is unavailable") from None

    @staticmethod
    def place(uow, scope):
        business = uow.repository("businesses").get(scope) if scope else None
        return {"business_id": scope, "business_slug": business["slug"] if business else None,
                "business_name": (business.get("name", business["slug"]) if business else "Global")}


__all__ = ["ABSENT", "HIDDEN", "HistoricalReferences", "SCOPED", "stored_namespace"]
