"""The synthetic host's Action and Signoff item kinds for the associated-records checks.

The composed host resolves both through the operations engine's neutral item lookup (``records.lookup.RecordItems`` and
``records.resources.RelatedResources``). These small lookups keep the same authorization points the host uses: the
Action's own read policy and business membership (``linked_action``), and the Signoff domain's review-scope read. No
product code, provider or live data."""

from __future__ import annotations

from qorqut_operations.actions.models import one_line
from qorqut_operations.actions.writeback import linked_action
from qorqut_operations.application.plans import ref
from qorqut_operations.contracts import DomainError, InvocationOptions
from qorqut_operations.items import authorize_item_read, item_revision


class ActionSignoffLookup:
    """Item kinds ``action`` and ``signoff`` over the engine's own stored records."""

    def __init__(self, host):
        self.host = host

    def resource(self, ctx, item, uow):
        kind, identity = item["type"], item["id"]
        if kind == "action":
            # Canonical action_id or a bare ID in this invocation's own storage, authorized by membership.
            row = linked_action(ctx, uow, identity)
            if row is None:
                raise DomainError("not_found", "Context item does not exist in this project")
            return "action_logs", row
        row = uow.repository("signoffs", business_id=ctx.business_id).get(identity)
        if row is None:
            raise DomainError("not_found", "Context item does not exist in this project")
        return "signoffs", row

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        table, row = self.resource(ctx, item, uow)
        if table == "action_logs":
            authorize_item_read(self.host.app, ctx, "actions_get", uow)
            if builder:
                builder.expect(ref(table, row["id"], row["business_id"], None if row["business_id"] else "global"), row)
            return {"label": one_line(row["action_text"]), "state": None, "revision": item_revision(row),
                    "resource_type": table, "resource_id": row["action_key"], "archived": False}
        # The Signoff domain's own read enforces review scope, visibility and the caller's read grant.
        view = self.host.app.registry.get("signoffs_get").read(ctx, {"signoff_id": row["id"]}, InvocationOptions(), uow)["signoff"]
        authorize_item_read(self.host.app, ctx, "signoffs_get", uow)
        if builder:
            builder.expect(ref(table, row["id"], row["review_business_id"]), row)
        return {"label": view["title"], "state": view["state"], "revision": view["status_revision"],
                "resource_type": table, "resource_id": row["id"], "archived": False}

    def relationship_item(self, ctx, item, uow):
        return item

    def fingerprint(self, ctx, item, uow, builder=None):
        return str(self.resource(ctx, item, uow)[1]["revision"])


__all__ = ["ActionSignoffLookup"]