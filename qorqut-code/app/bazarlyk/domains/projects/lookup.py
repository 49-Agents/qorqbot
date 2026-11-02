"""The neutral ``project`` item kind: how an existing Project resolves, is enumerated and fingerprinted.

Registering this kind grants nothing. ``resource`` keeps the Project's own current reach check and returns the
stored ``businesses`` row; ``resolve`` authorizes the Project record read (``project_record_get``) at the same
point the other neutral lookups use for their domain reads, so a Project item is readable only by a caller that
could read that Project right now. ``candidates`` enumerates the accessible Project directory of the caller's
scope: active Projects reached by current membership (humans) or current active-global reach (agents). Nothing
here reads a Project's records, and a Project item is never a Task, a Story or a second copy.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_operations.items import authorize_item_read

from . import access


class ProjectLookup:
    """``ItemLookup`` for ``project``: a descriptive endpoint over the one existing Project row."""

    def __init__(self, application):
        self.application = application

    def resource(self, ctx, item, uow):
        row = uow.repository("businesses").get(item["id"])
        if row is None or item["type"] != access.PROJECT_TYPE:
            raise DomainError("not_found", "Project does not exist")
        # Both identifiers of the canonical reference must agree: a Project is keyed by its actual UUID.
        if item.get("business_id") not in (None, row["id"]):
            raise DomainError("validation_error", "A project reference must name the project as its own scope")
        if not access.business_participant(ctx, row["id"], uow):
            raise DomainError("not_found", "Project is outside your current access")
        if row["archived_at"] is not None:
            # An archived Project stays stored with its links and conversation; it is not part of current work.
            raise DomainError("business_archived", "This project is archived")
        return "businesses", row

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        table, row = self.resource(ctx, item, uow)
        authorize_item_read(self.application, ctx, "project_record_get", uow)
        slug, name = row["slug"], row["name"]
        revision = row["revision"]
        if builder:
            from qorqut_operations.application.plans import ref
            builder.expect(ref(table, row["id"], None), row)
        return {**item, "business_id": row["id"], "label": name, "state": None,
                "revision": revision, "resource_type": table, "resource_id": row["id"], "subtype": "project",
                "archived": row["archived_at"] is not None, "summary": access.project_view(row)["summary"],
                "business_slug": slug, "business_name": name,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def relationship_item(self, ctx, item, uow):
        return {"type": access.PROJECT_TYPE, "id": item["id"], "business_id": item["id"]}

    def fingerprint(self, ctx, item, uow, builder=None):
        table, row = self.resource(ctx, item, uow)
        public = access.project_view(row)
        return canonical_hash({"content": public["name"], "summary": public["summary"], "slug": public["slug"]})

    def candidates(self, ctx, uow, kind=None):
        """The accessible existing Project directory of this scope; never a new or synthetic Project."""
        if kind and kind != access.PROJECT_TYPE:
            return
        # Reach is the shared rule, not a stored list: ask it for each active Project rather than duplicating it.
        for row in uow.connection.execute("SELECT id FROM businesses WHERE archived_at IS NULL ORDER BY id"):
            if access.business_participant(ctx, row["id"], uow):
                yield {"type": access.PROJECT_TYPE, "id": row["id"]}


__all__ = ["ProjectLookup"]