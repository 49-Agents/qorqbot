"""Retained-domain behaviour around the generic prepared-record application.

``apply_records`` is the Operations engine's; this is what the retained v1 domains add around it, in the same
transaction and at the same points the deployed ``domains.common.apply_records`` ran it: the updates that must advance an
aggregate's revision although its own fields are unchanged (``force_revision``) and the shared record history
(``record_applied``, for the ``tracked_tables`` whose before-image it needs). A failure in any of them propagates and
rolls the unit back."""

from __future__ import annotations

from bazarlyk.storage import history


class HostRecordEffects:
    tracked_tables = frozenset(history.TRACKED_TABLES)

    def apply_effect(self, context, plan, change, uow, repository, key, values):
        """No retained domain has a collection limit or an allocated sequence to enforce while applying a record."""
        return None

    def force_revision(self, plan, change):
        table = change.resource.resource_type
        operation = change.operation_name or plan.operation_name
        table_rows_changed = table == "data_tables" and any(
            child.resource.resource_type == "data_rows" and child.action != "policy_effect" for child in plan.changes)
        campaign_responsibility_changed = table == "tasks" and operation == "campaign_responsible_set"
        campaign_attribution_changed = table in {"signoffs", "documents"} and operation == "campaign_attribution_set"
        return (table_rows_changed or campaign_responsibility_changed
                or campaign_attribution_changed or table == "campaign_reply_batches")

    def record_applied(self, uow, plan, table, before, after, *, actor_id=None):
        history.record_applied(uow, plan, table, before, after, actor_id=actor_id)
