"""The neutral ``project`` item kind: how an existing Project resolves, is enumerated and fingerprinted.

Registering this kind grants nothing. Every read below passes the one nonrecursive Project guard of ``access.guard``:
the owner/exact configured Ordubashy identity, the Project's current membership or global reach, its archived state and
the ordinary ``project_record_get`` policy (explicit deny included). ``resolve`` and ``fingerprint`` therefore never
produce a label, summary or content hash for a Project the caller may not read right now.

``candidates`` enumerates the accessible Project directory of the caller's scope: active Projects reached by current
membership (humans) or current active-global reach (agents). Enumeration grants nothing; every candidate is resolved
through the same guard before a caller sees any detail. Nothing here reads a Project's records, and a Project item is
never a Task, a Story or a second copy.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError, canonical_hash

from . import access


class ProjectLookup:
    """``ItemLookup`` for ``project``: a descriptive endpoint over the one existing Project row."""

    def __init__(self, application):
        self.application = application

    def resource(self, ctx, item, uow):
        """The stored row after the one guard; an internal two-field reference is canonicalized, a mismatched one is not."""
        if item.get("type") != access.PROJECT_TYPE:
            raise DomainError("not_found", "Project does not exist")
        reference = access.canonical_reference(dict(item))
        _, row = access.guard(ctx, self.application, uow, project_id=reference["id"])
        return "businesses", row

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        reference = access.canonical_reference(dict(item))
        _, row = access.guard(ctx, self.application, uow, project_id=reference["id"])
        slug, name = row["slug"], row["name"]
        if builder:
            from qorqut_operations.application.plans import ref
            builder.expect(ref("businesses", row["id"], None), row)
        return {"type": access.PROJECT_TYPE, "id": row["id"], "business_id": row["id"], "label": name, "state": None,
                "revision": row["revision"], "resource_type": "businesses", "resource_id": row["id"], "subtype": "project",
                "archived": row["archived_at"] is not None, "summary": access.project_view(row)["summary"],
                "business_slug": slug, "business_name": name,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def relationship_item(self, ctx, item, uow):
        reference = access.canonical_reference(dict(item))
        access.guard(ctx, self.application, uow, project_id=reference["id"])
        return {"type": access.PROJECT_TYPE, "id": reference["id"], "business_id": reference["id"]}

    def fingerprint(self, ctx, item, uow, builder=None):
        view = self.resolve(ctx, item, uow, builder=builder)
        return canonical_hash({"content": view["label"], "summary": view["summary"], "slug": view["business_slug"]})

    def candidates(self, ctx, uow, kind=None):
        """The accessible existing Project directory of this scope; never a new or synthetic Project."""
        if kind and kind != access.PROJECT_TYPE:
            return
        # Reach is the shared rule, not a stored list: ask it for each active Project rather than duplicating it.
        for row in uow.connection.execute("SELECT id FROM businesses WHERE archived_at IS NULL ORDER BY id"):
            if access.business_participant(ctx, row["id"], uow):
                yield {"type": access.PROJECT_TYPE, "id": row["id"]}


__all__ = ["ProjectLookup"]
