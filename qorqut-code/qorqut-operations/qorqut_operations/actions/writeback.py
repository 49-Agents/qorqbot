"""Automatic Action writebacks over the engine's Action writer.

Actions (their rows, immutable revisions, tags, discussion and the admission of each revision) are the engine's. A host's delivery
paths record a confirmed external result as one automatic business-operational Action inside the receipt transaction;
this adapter owns the one-source-one-Action idempotency key. The caller's applying operation must settle the plan
(``qorqut_operations.actions.settle`` with the composition's ``on_revision``) so the revision is admitted and announced in
the same transaction.

Import this module directly (``qorqut_operations.actions.writeback``): the ``actions`` package initializer does not
load it, so the writer never imports it back."""

from __future__ import annotations

import re

from qorqut_operations import local_time
from qorqut_operations.actions import links, models, new_action, reader_scope, visible
from qorqut_operations.application.plans import ref
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.tags.catalog import distinct_labels


class ActionWriteback:
    def __init__(self, *, clock):
        self.clock = clock

    def append_to_plan(self, builder, uow, *, text, tag, refs=None, operation_name=None, source_key=None, occurred_at=None):
        """One canonical business-operational Action; a retried confirmed ``source_key`` records it once.

        ``text`` is the concise result, ``tag`` its ordinary label and ``refs`` the named evidence strings."""
        refs = refs or {}
        [tag] = distinct_labels([tag])
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) or not isinstance(value, str) or not value for key, value in refs.items()):
            raise DomainError("validation_error", "Activity references must be named non-empty strings")
        business_id = builder.ctx.business_id
        if business_id is None:
            raise DomainError("scope_required", "Activity entries belong to a selected project")
        if builder.ctx.home_business_id is not None and business_id != builder.ctx.home_business_id:
            raise DomainError("forbidden", "Project agents record actions only for their home project")
        if refs.get("task"):
            task = uow.repository("tasks", business_id=business_id).get(refs["task"], required=True)
            builder.expect(ref("tasks", task["id"], business_id), task)
        identity = None
        if source_key is not None:
            identity = models.source_identity(business_id, source_key)
            existing = uow.repository("action_logs", business_id=business_id).get(identity)
            if existing is not None:
                return existing                      # A retried confirmed source already has its one canonical Action.
        occurrence = (local_time.recorded_time(builder.at) if not occurred_at
                      else {**local_time.recorded_time(occurred_at), "occurred_at": occurred_at})
        origin = models.origin_for(builder.ctx)
        return new_action(builder, uow, text=models.concise(text) if origin == "automatic" else text,
                          category="business-operational", businesses=[business_id], occurrence=occurrence, origin=origin,
                          refs=refs, tags=[tag], operation_name=operation_name, identity=identity)


def linked_action(ctx, uow, identity):
    """Resolve an Action item for the invocation's workspace, or None when it is not visible there.

    ``identity`` is a canonical ``action_id``, or a bare ID in the invocation's own storage (so equal IDs in different
    businesses never collide). A business sees the Actions linked to it, wherever they are stored; a Global context
    sees the Global-stored Actions its Action scope allows. The row keeps its true storage namespace."""
    if identity.startswith(("global/", "business/")):
        row = decode_row(uow.connection.execute("SELECT * FROM action_logs WHERE action_key=?", (identity,)).fetchone())
    else:
        row = uow.repository("action_logs", business_id=ctx.business_id).get(identity)
    if row is None:
        return None
    row_links = links(uow, row["action_key"])
    if ctx.business_id is not None:
        return row if ctx.business_id in row_links else None
    try:
        scope = reader_scope(ctx, uow)
    except DomainError:
        return None
    return row if row["business_id"] is None and visible(scope, row, row_links) else None


def linked_action_items(ctx, uow, maximum=20000):
    """Item IDs of the Actions a business can link: its own rows by bare ID, shared Global rows by action_id."""
    if ctx.business_id is None:
        found = uow.connection.execute("SELECT id FROM action_logs WHERE business_id IS NULL LIMIT ?", (maximum + 1,)).fetchall()
    else:
        found = uow.connection.execute(
            "SELECT CASE WHEN a.business_id=? THEN a.id ELSE a.action_key END FROM action_logs a "
            "JOIN action_businesses m ON m.action_key=a.action_key WHERE m.business_id=? LIMIT ?",
            (ctx.business_id, ctx.business_id, maximum + 1)).fetchall()
    if len(found) > maximum:
        raise DomainError("result_too_large", "Narrow the query period or filters")
    return [row[0] for row in found]


__all__ = ["ActionWriteback", "linked_action", "linked_action_items"]
