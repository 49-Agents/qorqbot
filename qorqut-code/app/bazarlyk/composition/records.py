"""Retained-domain behaviour around the generic prepared-record application.

``apply_records`` is the Operations engine's; this is what the retained v1 domains add around it, in the same
transaction and at the same points the deployed ``domains.common.apply_records`` ran it: Story event allocation,
collection limits and the frozen pre-101 Action insert (``apply_effect``), the updates that must advance an
aggregate's revision although its own fields are unchanged (``force_revision``), and the shared record history
(``record_applied``, for the ``tracked_tables`` whose before-image it needs). A failure in any of them propagates and
rolls the unit back."""

from __future__ import annotations

from qorqut_operations.application.plans import record_key, timestamp
from qorqut_operations.contracts import DomainError

from bazarlyk.storage import history

# Collection limits re-enforced inside the applying transaction: preparation can precede approval by hours.
STORY_LIMITS = {"story_drafts": (20, "drafts")}


class HostRecordEffects:
    tracked_tables = frozenset(history.TRACKED_TABLES)

    def apply_effect(self, context, plan, change, uow, repository, key, values):
        table = change.resource.resource_type
        if change.action == "append_story_event" and table == "story_events":
            # Content, identity, attribution and parent are frozen in the prepared plan.
            # Sequence and commit time are allocated only inside the applying transaction.
            # Advancing the timeline does not invalidate a pending edit to story details.
            story = uow.repository("stories", business_id=change.resource.business_id).get(values["story_id"], required=True)
            committed_at = timestamp()
            values.update(sequence=story["sequence"] + 1, created_at=committed_at)
            row = repository.insert(values)
            history.record_applied(uow, plan, "story_events", None, row)
            uow.connection.execute("UPDATE stories SET sequence=?, updated_at=? WHERE business_id IS ? AND id=?",
                (values["sequence"], committed_at, change.resource.business_id, story["id"]))
            return [row]
        if change.action == "insert":
            if table in STORY_LIMITS:
                limit, label = STORY_LIMITS[table]
                predicate = "business_id IS ? AND story_id=? AND author_id=?"
                parameters = [change.resource.business_id, values["story_id"], values["author_id"]]
                count = uow.connection.execute(f"SELECT count(*) FROM {table} WHERE {predicate}", parameters).fetchone()[0]
                if count >= limit:
                    raise DomainError("validation_error", f"A story supports up to {limit} {label} per author")
            from bazarlyk.domains.activity import compatibility as legacy_action
            if legacy_action.is_pre_101_insert(change.resource, values):
                # A frozen pre-101 action insert: canonical columns and its one membership are
                # derived here, in the same transaction; the prepared payload is never rewritten.
                business_id = change.resource.business_id
                row = repository.insert(legacy_action.canonical_values(business_id, values))
                history.record_applied(uow, plan, table, None, row)
                link = uow.repository("action_businesses").insert(legacy_action.membership(business_id, values))
                history.record_applied(uow, plan, "action_businesses", None, link)
                return [row]
        return None

    def force_revision(self, plan, change):
        table = change.resource.resource_type
        operation = change.operation_name or plan.operation_name
        # Task comments and workflow edges change the aggregate even when its fields are unchanged.
        task_children_changed = table == "tasks" and any(
            child.action != "policy_effect" and (
                child.resource.resource_type == "task_notes" and child.values.get("task_id") == record_key(change.resource)
                or child.resource.resource_type == "relationships"
            ) for child in plan.changes)
        table_rows_changed = table == "data_tables" and any(
            child.resource.resource_type == "data_rows" and child.action != "policy_effect" for child in plan.changes)
        campaign_responsibility_changed = table == "stories" and operation == "stories_campaign_responsible_set"
        campaign_attribution_changed = table in {"signoffs", "documents"} and operation == "campaign_attribution_set"
        return (task_children_changed or table_rows_changed or campaign_responsibility_changed
                or campaign_attribution_changed or table == "campaign_reply_batches")

    def record_applied(self, uow, plan, table, before, after, *, actor_id=None):
        history.record_applied(uow, plan, table, before, after, actor_id=actor_id)
