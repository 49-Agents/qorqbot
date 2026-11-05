"""The shared discussion of a Table (ordinary or CRM): comments, follows and the membership developments.

This is the existing item discussion (``thread_messages`` / ``thread_follows``) with the ``table`` item type, written by
the same attribution, append-only and follow rules as Task and Action discussions. A comment emits its one inbox event
here; a change to the Table's data (a membership, a stage, a schema edit) is announced by the capture mapping instead, so a
semantic change is never announced twice.

A membership development is a system message whose ``change_json`` holds the structured reference
``{kind: "crm_membership", entry_id, record, operation, changed_fields, stage?}``. Its body is generic: it never copies a Task
title, profile or local cell value. Readers resolve the record reference under their own current authority and the
development is redacted down to its generic form when the Task is unavailable to them.
"""

from __future__ import annotations

from qorqut_operations.application.plans import PlanBuilder, apply_records, ref, require_revision, timestamp
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.conversations import participants
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.discussion.follows import ThreadItem, auto_follow, can_follow, drop_legacy_subscription, follow_state
from qorqut_operations.discussion.follows import followers as follower_ids
from qorqut_operations.discussion.threads import attach_event, message_view, plan_comment, read_messages, system_entry
from qorqut_operations.tables.operations import table_context, table_namespace
from qorqut_operations.team_channel import reject_owner_team_write

from .access import ScopeCache
from .refs import record_ref

BODIES = {"added": "An entry was added.", "updated": "An entry was updated.", "removed": "An entry was removed.",
          "restored": "An entry was restored."}
EVENT_TYPE = "note_added"


class Threads:
    def __init__(self, crm):
        self.crm = crm

    def item(self, ctx, table_id):
        return ThreadItem(ctx.business_id, "table", table_id)

    def builder(self, ctx, operation, args, title):
        return PlanBuilder(ctx, operation, args, title, clock=self.crm.clock, namespace=table_namespace(ctx, args))

    # -- reading ---------------------------------------------------------------------------------------------------

    def redact(self, ctx, message, uow, cache: ScopeCache):
        """A membership development, with its record reference resolved under the reader's own authority."""
        change = message.get("change")
        if not change or change.get("kind") != "crm_membership":
            return message
        reference = change["record"]
        row = (self.crm.access.row(reference["business_id"], reference["id"], uow)
               if self.crm.access.readable(ctx, reference["business_id"], reference["id"], uow, cache) else None)
        if row is None:
            return {**message, "change": {"kind": "crm_membership", "entry_id": change["entry_id"],
                                          "operation": change["operation"], "unavailable": True}}
        return {**message, "change": {**change, "record_label": row["title"]}}

    def read(self, ctx, args, uow):
        ctx = table_context(ctx, args, uow)
        table = self.crm.tables.table(ctx, args["table_id"], uow)
        item = self.item(ctx, table["id"])
        history = read_messages(uow, item, args, "tables_thread_get", ctx.principal_id,
                                resolve_refs=self.crm.tasks.message_refs(ctx, uow))
        cache = ScopeCache()
        messages = [self.redact(ctx, message, uow, cache) for message in history["messages"]]
        people = {table["created_by"], *(message["created_by"] for message in messages)}
        state = follow_state(uow, item, ctx.principal_id)
        return {"thread_id": history["thread_id"], "table_id": table["id"], "participants": participants(uow, people),
                "messages": messages, "next_cursor": history["next_cursor"], "follow": state, "following": state["following"],
                "follow_reason": state["reason"]}

    # -- comments --------------------------------------------------------------------------------------------------

    def prepare_comment(self, ctx, args, options, uow):
        ctx = table_context(ctx, args, uow)
        table = self.crm.tables.table(ctx, args["table_id"], uow)
        if options.expected_revision is not None:
            require_revision(table, options.expected_revision)
        builder = self.builder(ctx, "tables_comment", args, "Comment on table")
        builder.expect(ref("data_tables", table["id"], ctx.business_id), table)
        plan_comment(ctx, uow, builder, self.item(ctx, table["id"]), note=args["text"], summary=args.get("summary"),
                     refs=args.get("note_refs", []), relay=args.get("relay"),
                     resolve=lambda refs: self.crm.tasks.note_refs(ctx, refs, uow, builder),
                     guard=lambda: reject_owner_team_write(ctx, uow, "tables_comment"))
        builder.effects.append("Adds a message to the table's shared discussion; the table's data and revision are unchanged. "
                               "An archived table can still be discussed.")
        return builder.build()

    def validate_comment(self, ctx, plan, uow):
        """Re-derive the comment's Table and its typed references from current state when it is approved or applied."""
        self.crm.tables.validate_scope(ctx, plan, uow)
        ctx = table_context(ctx, plan.arguments, uow)
        table = self.crm.tables.table(ctx, plan.arguments["table_id"], uow)
        for change in plan.changes:
            if change.resource.resource_type == "thread_messages" and (
                    change.values["item_type"], change.values["item_id"]) != ("table", table["id"]):
                raise DomainError("invalid_plan", "The comment must belong to the table the request names")
        self.crm.tasks.note_refs(ctx, plan.arguments.get("note_refs", ()), uow, strict=True)

    def validate_comment_replay(self, ctx, args, replay, uow):
        """A saved comment result (with its resolved references) is returned only while those are still permitted."""
        if replay.status != "ok":
            return
        ctx = table_context(ctx, args, uow)
        self.crm.recheck(ctx, "tables_comment", table_namespace(ctx, args), uow)
        self.crm.tables.table(ctx, args["table_id"], uow)
        self.crm.tasks.note_refs(ctx, args.get("note_refs", ()), uow, strict=True)

    def validate_follow_replay(self, ctx, args, replay, uow):
        if replay.status != "ok":
            return
        ctx = table_context(ctx, args, uow)
        self.crm.recheck(ctx, "tables_follow", table_namespace(ctx, args), uow)
        self.crm.tables.table(ctx, args["table_id"], uow)

    def apply_comment(self, ctx, plan, uow):
        ctx = table_context(ctx, plan.arguments, uow)
        messages = [plain(change.values) for change in plan.changes if change.resource.resource_type == "thread_messages"]
        if messages:
            reject_owner_team_write(ctx, uow, "tables_comment")
        apply_records(ctx, plan, uow)
        table = self.crm.tables.table(ctx, plan.arguments["table_id"], uow)
        item = self.item(ctx, table["id"])
        for comment in messages:
            auto_follow(uow, item, [comment["recorder_id"]], "commenter")
            if comment["author_id"] != comment["recorder_id"]:
                auto_follow(uow, item, [comment["author_id"]], "represented")
        # The comment's one event, delivered to the optional followers; Table data changes are announced by the capture mapping.
        event = emit_event(uow, business_id=ctx.business_id, source_type="data_tables", source_id=table["id"],
                           source_revision=table["revision"], actor_id=ctx.principal_id, event_type=EVENT_TYPE,
                           summary=table["title"], optional=follower_ids(uow, item))
        if event is not None:
            attach_event(uow, item, [message["id"] for message in messages], event)
        row = uow.connection.execute("SELECT * FROM thread_messages WHERE business_id IS ? AND id=?",
                                     (ctx.business_id, messages[0]["id"])).fetchone()
        return {"table": self.crm.tables.view(ctx, table, uow), "thread_id": item.thread_id,
                "message": message_view(row, uow, self.crm.tasks.message_refs(ctx, uow))}

    # -- follows ---------------------------------------------------------------------------------------------------

    def prepare_follow(self, ctx, args, options, uow):
        ctx = table_context(ctx, args, uow)
        table = self.crm.tables.table(ctx, args["table_id"], uow)
        item = self.item(ctx, table["id"])
        if not can_follow(uow, item, ctx.principal_id):
            raise DomainError("forbidden", "Only an active participant of this namespace can follow its items")
        builder = self.builder(ctx, "tables_follow", args, "Follow table" if args["following"] else "Unfollow table")
        builder.expect(ref("data_tables", table["id"], ctx.business_id), table)
        key = {"item_type": "table", "item_id": table["id"], "principal_id": ctx.principal_id}
        existing = uow.repository("thread_follows", business_id=ctx.business_id).get(key)
        values = {"following": int(args["following"]), "explicit": 1, "reason": "explicit", "updated_at": builder.at}
        if existing:
            builder.record("thread_follows", key, existing, values, label="follow")
        else:
            builder.record("thread_follows", key, None, {**key, **values, "created_at": builder.at}, label="follow")
        builder.effects.append("Changes only your own optional subscription to this table's discussion.")
        return builder.build()

    def apply_follow(self, ctx, plan, uow):
        ctx = table_context(ctx, plan.arguments, uow)
        apply_records(ctx, plan, uow)
        table_id = plan.arguments["table_id"]
        item = self.item(ctx, table_id)
        if not plan.arguments["following"]:
            drop_legacy_subscription(uow, item, ctx.principal_id)  # one preference, whichever surface changed it
        return {"table_id": table_id, **follow_state(uow, item, ctx.principal_id)}

    # -- membership developments -----------------------------------------------------------------------------------

    def record_membership(self, ctx, uow, before, after):
        """Append the structured development of one membership change to the Table's discussion; returns its id."""
        if before is None:
            operation = "added"
        elif before["removed"] != after["removed"]:
            operation = "removed" if after["removed"] else "restored"
        else:
            operation = "updated"
        old_cells = before["cells_json"] if before else {}
        keys = set(old_cells) | set(after["cells_json"])
        changed = sorted(key for key in keys if old_cells.get(key) != after["cells_json"].get(key) or (
            before is None and key in after["cells_json"]))
        old_stage = before["stage_id"] if before else None
        change = {"kind": "crm_membership", "entry_id": after["id"],
                  "record": record_ref(after["record_business_id"], after["record_task_id"]),
                  "operation": operation, "changed_fields": changed}
        if after["stage_id"] != old_stage:
            change["stage"] = {"from": old_stage, "to": after["stage_id"]}
        return system_entry(uow, self.item(ctx, after["table_id"]), recorder_id=ctx.principal_id, body=BODIES[operation], change=change,
                        at=timestamp(self.crm.clock))
