"""CRM memberships: bounded reads and the prepared add / update / remove / revive writes.

A membership points at an existing Task by its exact scoped identity and stores only local values. Every read rechecks
the caller's authority over the Task in its own scope; an unavailable Task yields a bare placeholder. Every write is one
prepared change set on the Table: it takes the Table's expected revision, bumps it once and captures one exact snapshot.
Creating the Task of a new entry reuses ``tasks_create``'s own prepare and apply inside that same plan and transaction.
"""

from __future__ import annotations

from dataclasses import replace
import json

from qorqut_operations.application.plans import PlanBuilder, apply_records, page, ref, require_revision, timestamp
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.tables import crm_model
from qorqut_operations.tables.operations import MAX_ROWS, cells_checked, columns_checked, table_context, table_namespace

from .access import ScopeCache
from .refs import namespace_of, record_ref

PAGE_MAXIMUM = 100
# Entries one ``crm_entries_update`` may change together, and the only fields such a change may write.
MAX_UPDATES = 100
UPDATE_FIELDS = frozenset({"cells_json", "stage_id", "updated_by", "updated_at"})
# What belongs to the Table's side of a composed plan; everything else is the new Task's own effects.
TABLE_SIDE = frozenset({"data_tables", "crm_entries", "table_versions", "jobs"})


def table_side(change):
    kind = change.resource.resource_type
    if kind in ("thread_messages", "thread_follows"):
        return change.values.get("item_type") == "table"
    return kind in TABLE_SIDE


class Entries:
    def __init__(self, crm):
        self.crm = crm

    # -- shared lookups --------------------------------------------------------------------------------------------

    def table(self, ctx, table_id, uow):
        """The Table, which must be a CRM (a general table has no memberships)."""
        table = self.crm.tables.table(ctx, table_id, uow)
        if table["table_kind"] != crm_model.CRM:
            raise DomainError("validation_error", "This table is a general table; CRM entries belong to a CRM table")
        return table

    def writable(self, ctx, table_id, options, uow):
        table = self.table(ctx, table_id, uow)
        require_revision(table, options.expected_revision)
        if table["archived"]:
            raise DomainError("validation_error", "Unarchive the table before changing its entries")
        return table

    @staticmethod
    def split_columns(table):
        local = {column["id"]: column for column in table["columns_json"] if column.get("record_field") is None}
        projected = {column["id"]: column for column in table["columns_json"] if column.get("record_field") is not None}
        return local, projected

    def cells_for_write(self, table, cells):
        """Local values only: a projected key is refused explicitly, unknown keys and type violations by the cell rules."""
        local, projected = self.split_columns(table)
        shown = sorted(set(cells) & projected.keys())
        if shown:
            raise DomainError("validation_error", "These columns show Task fields; edit the record itself with tasks_update",
                              details={"columns": shown})
        cells_checked(cells, columns_checked(list(local.values())) if local else {})
        return cells

    @staticmethod
    def find(ctx, table_id, entry_id, uow):
        entry = uow.repository("crm_entries", business_id=ctx.business_id).get(entry_id)
        if entry is None or entry["table_id"] != table_id:
            raise DomainError("not_found", "Entry does not belong to this table")
        return entry

    @staticmethod
    def lookup(ctx, table_id, reference, uow):
        found = uow.connection.execute(
            "SELECT * FROM crm_entries WHERE business_id IS ? AND table_id=? AND record_namespace=? AND record_task_id=?",
            (ctx.business_id, table_id, namespace_of(reference["business_id"]), reference["id"])).fetchone()
        return decode_row(found)

    @staticmethod
    def reference(entry):
        return record_ref(entry["record_business_id"], entry["record_task_id"])

    def builder(self, ctx, operation, args, title):
        return PlanBuilder(ctx, operation, args, title, clock=self.crm.clock, namespace=table_namespace(ctx, args))

    # -- reads -----------------------------------------------------------------------------------------------------

    def placeholder(self, entry):
        """All a reader without access to the Task learns: that a membership exists. No identity, scope, stage or value."""
        return {"id": entry["id"], "revision": entry["revision"], "position": entry["position"], "unavailable": True,
                "removed": bool(entry["removed"])}

    def view(self, ctx, table, entry, uow, cache: ScopeCache, now=None):
        reference = self.reference(entry)
        access = self.crm.access
        row = access.row(entry["record_business_id"], entry["record_task_id"], uow) if access.readable(
            ctx, entry["record_business_id"], entry["record_task_id"], uow, cache) else None
        if row is None:
            return self.placeholder(entry)
        record = access.view(ctx, row, uow, cache, now)
        cells, errors = access.render(entry, table["columns_json"], record,
                                      access.definitions(ctx, entry["record_business_id"], entry["record_task_id"], uow, cache))
        local = {column["id"] for column in table["columns_json"] if column.get("record_field") is None}
        return {"id": entry["id"], "table_id": entry["table_id"], "business_id": entry["business_id"],
                "revision": entry["revision"], "position": entry["position"], "stage_id": entry["stage_id"], "cells": cells,
                "local_cells": {key: value for key, value in entry["cells_json"].items() if key in local},
                "removed": bool(entry["removed"]), "created_at": entry["created_at"], "updated_at": entry["updated_at"],
                "record": record, "record_ref": reference, **({"cell_errors": errors} if errors else {})}

    def list_entries(self, ctx, table, args, uow, operation="crm_entries_list"):
        """One bounded page. Authorization precedes every projection, filter and sort of a Task-sourced value."""
        if operation != "crm_entries_list":
            # The generic row read of a CRM is this same read: the dedicated operation's effective policy (and a delegated
            # assignment's allowance of it) applies too, so the alias can never open what a deny on it would close.
            application = self.crm.application
            application.authorize(ctx, application.registry.get("crm_entries_list"), (), uow, namespace=table_namespace(ctx, args),
                                  arguments=args if ctx.delegation_assignment_id is not None else None)
        columns = table["columns_json"]
        local, projected = self.split_columns(table)
        equals, order_by = args.get("equals", {}), args.get("order_by")
        shown = sorted(set(equals) & projected.keys())
        if shown:
            raise DomainError("validation_error", "Filter on local columns only; these columns show Task fields",
                              details={"columns": shown})
        cells_checked(equals, columns_checked(list(local.values())) if local else {})
        if order_by is not None:
            column = local.get(order_by) or projected.get(order_by)
            if column is None or column["type"] == "json" or order_by in projected:
                raise DomainError("validation_error", "Sort by a local text, number, boolean or date column; "
                                  "Task fields are not sortable because a Task edit does not change the Table")
        if args.get("stage_id") is not None and args["stage_id"] not in crm_model.stage_ids(table["crm_config_json"]):
            raise DomainError("validation_error", "The stage is not configured on this CRM", details={"stage_id": args["stage_id"]})
        args = {**args, "limit": min(args.get("limit", 50), PAGE_MAXIMUM)}
        cache, query = ScopeCache(), (args.get("query") or "").casefold()
        removed = "" if args.get("include_removed") else " AND e.removed=0"
        found = [decode_row(row) for row in uow.connection.execute(
            "SELECT e.*, t.id AS _task, t.archived AS _archived FROM crm_entries e "
            "LEFT JOIN tasks t ON t.business_id IS e.record_business_id AND t.id=e.record_task_id "
            "WHERE e.business_id IS ? AND e.table_id=?" + removed + " LIMIT ?", (ctx.business_id, table["id"], MAX_ROWS + 1))]
        if len(found) > MAX_ROWS:
            raise DomainError("result_too_large", "Narrow the query period or filters")
        filtered = bool(query or equals or args.get("stage_id"))
        selected = []
        for entry in found:
            available = entry["_available"] = (entry["_task"] is not None
                                               and self.crm.access.readable(ctx, entry["record_business_id"], entry["record_task_id"],
                                                                            uow, cache))
            if not available:
                if not filtered:  # an unavailable target can never match a filter without disclosing its values
                    selected.append(entry)
                continue
            if entry["_archived"] and not args.get("include_archived"):
                continue
            if args.get("stage_id") and entry["stage_id"] != args["stage_id"]:
                continue
            if any(key not in entry["cells_json"] or entry["cells_json"][key] != value for key, value in equals.items()):
                continue
            if query and not self.matches(ctx, entry, table, uow, cache, query):
                continue
            selected.append(entry)
        key = lambda entry: (entry["position"], entry["id"])
        if order_by:
            descending = args.get("descending", False)

            def key(entry):
                # A hidden entry's local values never decide its place: those entries follow the readable ones, by position.
                if not entry["_available"]:
                    return (0 if descending else 1), False, "", entry["position"], entry["id"]
                value = entry["cells_json"].get(order_by)
                return (1 if descending else 0), value is not None, value if value is not None else "", entry["position"], entry["id"]
        chosen, cursor = page(selected, {**args, "table_revision": table["revision"]}, ctx.business_id, operation, key=key,
                              reverse=args.get("descending", False))
        now = timestamp(self.crm.clock)
        return {"items": [self.view(ctx, table, entry, uow, cache, now) for entry in chosen], "next_cursor": cursor,
                "table_revision": table["revision"]}

    def matches(self, ctx, entry, table, uow, cache, query):
        """Whether the query is in the entry's title, its local values or the Task fields its columns show."""
        access = self.crm.access
        row = access.row(entry["record_business_id"], entry["record_task_id"], uow)
        record = access.source(row, uow, cache)
        cells, _ = access.render(entry, table["columns_json"], record,
                                 access.definitions(ctx, entry["record_business_id"], entry["record_task_id"], uow, cache))
        return query in (record["title"] + " " + json.dumps(list(cells.values()), ensure_ascii=False)).casefold()

    # -- writes ----------------------------------------------------------------------------------------------------

    def prepare(self, operation, ctx, args, options, uow):
        ctx = table_context(ctx, args, uow)
        builder = self.builder(ctx, operation, args, operation.replace("_", " ").title())
        table = self.writable(ctx, args["table_id"], options, uow)
        resource = ref("data_tables", table["id"], ctx.business_id)
        builder.expect(resource, table)
        mark = len(builder.changes)
        if operation == "crm_entries_add":
            self.prepare_add(ctx, builder, table, args, options, uow)
        elif operation == "crm_entries_update":
            self.prepare_update(ctx, builder, table, args, uow)
        else:
            self.prepare_remove(ctx, builder, table, args, uow)
        if len(builder.changes) > mark:
            # One changing batch: the Table takes one revision and one exact snapshot, as a row batch does.
            builder.record("data_tables", table["id"], table, {"updated_at": builder.at}, label=table["title"])
            self.crm.tables.plan_version(builder, table["id"], uow)
        builder.effects.append("Changes only this Table's membership of existing Task records; the Tasks keep their type, parent, "
                               "state, assignees, thread and history.")
        return builder.build()

    def prepare_add(self, ctx, builder, table, args, options, uow):
        if ("record" in args) == ("create" in args):
            raise DomainError("validation_error", "Name exactly one of record (an existing Task) or create (a new Task)")
        cells = self.cells_for_write(table, args.get("cells", {}))
        config = table["crm_config_json"]
        count = uow.connection.execute("SELECT count(*) FROM crm_entries WHERE business_id IS ? AND table_id=?",
                                       (ctx.business_id, table["id"])).fetchone()[0]
        if "create" in args:
            stage = crm_model.stage_checked(config, args.get("stage_id"))
            task_id = self.compose_task(ctx, builder, args, options, uow)
            reference = record_ref(ctx.business_id, task_id)
            entry = None
        else:
            reference = args["record"]
            self.crm.access.require(ctx, reference, uow, builder)
            entry = self.lookup(ctx, table["id"], reference, uow)
            if entry is not None and not entry["removed"]:
                self.agree(entry, cells, args)
                builder.expect(ref("crm_entries", entry["id"], ctx.business_id), entry)
                return  # already a member with nothing conflicting: unchanged, no revision, no version, no event
            stage = (crm_model.stage_checked(config, args["stage_id"], current=entry["stage_id"] if entry else None)
                     if "stage_id" in args else (entry["stage_id"] if entry else None))
        if entry is not None:
            # Restoring the same membership keeps its identity, position and local data unless values are supplied.
            merged = {**entry["cells_json"], **cells}
            builder.record("crm_entries", entry["id"], entry, {"removed": 0, "cells_json": merged, "stage_id": stage,
                           "updated_by": ctx.principal_id, "updated_at": builder.at}, label=entry["id"])
            return
        if count >= MAX_ROWS:
            raise DomainError("validation_error", f"A CRM supports up to {MAX_ROWS} entries")
        position = uow.connection.execute("SELECT COALESCE(MAX(position),-1)+1 FROM crm_entries WHERE business_id IS ? AND table_id=?",
                                          (ctx.business_id, table["id"])).fetchone()[0]
        identity = new_id()
        builder.record("crm_entries", identity, None, {
            "id": identity, "table_id": table["id"], "record_task_id": reference["id"], "record_business_id": reference["business_id"],
            "record_namespace": namespace_of(reference["business_id"]), "position": position, "stage_id": stage, "cells_json": cells,
            "removed": 0, "created_by": ctx.principal_id, "updated_by": ctx.principal_id, "created_at": builder.at,
            "updated_at": builder.at}, label=identity)

    @staticmethod
    def agree(entry, cells, args):
        """An active member returns unchanged unless the caller supplied values that differ (then: update, never overwrite)."""
        differs = [key for key, value in cells.items() if key not in entry["cells_json"] or entry["cells_json"][key] != value]
        if differs or ("stage_id" in args and args["stage_id"] != entry["stage_id"]):
            raise DomainError("conflict", "The record is already an entry with different values; use crm_entries_update",
                              details={"entry_id": entry["id"], "columns": sorted(differs)})

    def task_arguments(self, ctx, args):
        """The ``tasks_create`` arguments of a new entry: a Customer (person) unless another Task type is chosen."""
        # A row is a record of its Table, so a second row with the same name is the Table's business, not a duplicate Task.
        create = {"task_type": "customer", "allow_duplicate": True, **args["create"]}
        return {**create, **({"scope": "global"} if ctx.business_id is None else {})}

    def compose_task(self, ctx, builder, args, options, uow):
        """Prepare the new Task with the Task service's own validation and adopt its effects into the Table's plan."""
        prepared = self.crm.tasks.prepare("tasks_create", ctx, self.task_arguments(ctx, args), options, uow)
        for target in prepared.expected_targets:
            if builder.targets.get(target.resource, target) != target:
                raise DomainError("invalid_plan", "Prepared snapshots disagree")
            builder.targets[target.resource] = target
        builder.changes.extend(prepared.changes)
        preview = plain(prepared.preview)  # a frozen mapping once built; its entries re-enter the Table's own preview unchanged
        builder.resources.extend(preview["resources"])
        builder.previews.extend(preview["changes"])
        builder.effects.extend(preview["effects"])
        task_id = next(change.resource.resource_id for change in prepared.changes
                       if change.resource.resource_type == "tasks" and change.action == "insert")
        # The new Task is judged by the Task service's own policy too: both authority sets must allow the whole change.
        created = next(change.resource for change in prepared.changes
                       if change.resource.resource_type == "tasks" and change.action == "insert")
        builder.policy_effect(created, "tasks_create", "Creates the Task of the new entry")
        return task_id

    @staticmethod
    def update_items(args):
        """``[(entry_id, changes)]`` of an update: exactly one entry (``entry_id`` + ``changes``) or ``updates``, each entry once."""
        if "updates" not in args:
            if "entry_id" not in args or "changes" not in args:
                raise DomainError("validation_error", "Name entry_id with changes, or updates")
            return [(args["entry_id"], args["changes"])]
        if "entry_id" in args or "changes" in args:
            raise DomainError("validation_error", "Name either entry_id with changes or updates, not both")
        identities = [item["entry_id"] for item in args["updates"]]
        repeated = sorted({identity for identity in identities if identities.count(identity) > 1})
        if repeated:
            raise DomainError("validation_error", "Each entry can appear only once in updates", details={"entry_ids": repeated})
        return [(item["entry_id"], item["changes"]) for item in args["updates"]]

    def prepare_update(self, ctx, builder, table, args, uow):
        for entry_id, changes in self.update_items(args):
            self.prepare_entry_update(ctx, builder, table, entry_id, changes, uow)

    def prepare_entry_update(self, ctx, builder, table, entry_id, changes, uow):
        entry = self.find(ctx, table["id"], entry_id, uow)
        if entry["removed"]:
            raise DomainError("validation_error", "This entry was removed; add the record again to restore it")
        self.crm.access.require(ctx, self.reference(entry), uow, builder)  # editing needs the caller to read the Task
        cells = {**entry["cells_json"], **self.cells_for_write(table, changes.get("cells", {}))}
        stage = (crm_model.stage_checked(table["crm_config_json"], changes["stage_id"], current=entry["stage_id"])
                 if "stage_id" in changes else entry["stage_id"])
        if cells == entry["cells_json"] and stage == entry["stage_id"]:
            builder.expect(ref("crm_entries", entry["id"], ctx.business_id), entry)
            return
        builder.record("crm_entries", entry["id"], entry, {"cells_json": cells, "stage_id": stage,
                       "updated_by": ctx.principal_id, "updated_at": builder.at}, label=entry["id"])

    def prepare_remove(self, ctx, builder, table, args, uow):
        entry = self.find(ctx, table["id"], args["entry_id"], uow)
        if entry["removed"]:
            builder.expect(ref("crm_entries", entry["id"], ctx.business_id), entry)
            return
        builder.record("crm_entries", entry["id"], entry, {"removed": 1, "updated_by": ctx.principal_id,
                       "updated_at": builder.at}, label=entry["id"])

    # -- applying --------------------------------------------------------------------------------------------------

    def split(self, plan):
        table_changes = tuple(change for change in plan.changes if table_side(change))
        return table_changes, tuple(change for change in plan.changes if not table_side(change))

    def validate(self, ctx, plan, uow):
        """Re-derive scope, authority and the plan's own consistency from current state, never from the plan's say-so."""
        self.crm.tables.validate_scope(ctx, plan, uow)
        ctx = table_context(ctx, plan.arguments, uow)
        args = plan.arguments
        table = self.table(ctx, args["table_id"], uow)
        if table["archived"] and any(change.resource.resource_type == "crm_entries" for change in plan.changes):
            raise DomainError("validation_error", "Unarchive the table before changing its entries")
        table_changes, task_changes = self.split(plan)
        if any((change.resource.resource_type == "data_tables" and change.resource.resource_id != table["id"])
               or (change.resource.resource_type in {"table_versions", "crm_entries"}
                   and change.values.get("table_id", table["id"]) != table["id"]) for change in table_changes):
            raise DomainError("invalid_plan", "A CRM change set can only change its own table")
        entry = next((change for change in table_changes if change.resource.resource_type == "crm_entries"
                      and change.action in {"insert", "update"}), None)
        if "create" in args:
            task_plan = replace(plan, operation_name="tasks_create", arguments=self.task_arguments(ctx, args), changes=task_changes)
            self.crm.tasks.validate_prepared(ctx, task_plan, uow)
            created = next((c.resource.resource_id for c in task_changes if c.resource.resource_type == "tasks" and c.action == "insert"), None)
            if created is None or entry is None or entry.action != "insert" or (
                    entry.values["record_task_id"], entry.values["record_business_id"]) != (created, ctx.business_id):
                raise DomainError("invalid_plan", "The new entry must point at the Task this change creates")
            return
        if task_changes:
            raise DomainError("invalid_plan", "Only creating an entry may change Tasks")
        if "record" in args:
            reference = args["record"]
            self.crm.access.require(ctx, reference, uow)
            if entry is not None and (entry.values.get("record_task_id", reference["id"]),
                                      entry.values.get("record_business_id", reference["business_id"])) != (
                                          reference["id"], reference["business_id"]):
                raise DomainError("invalid_plan", "The entry must point at the Task the request names")
            return
        if "updates" in args:
            self.validate_updates(ctx, plan, table, table_changes, uow)
            return
        current = self.find(ctx, table["id"], args["entry_id"], uow)
        if entry is not None and entry.resource.resource_id != current["id"]:
            raise DomainError("invalid_plan", "The change must edit the entry the request names")
        if plan.operation_name == "crm_entries_update":
            self.crm.access.require(ctx, self.reference(current), uow)

    def validate_updates(self, ctx, plan, table, table_changes, uow):
        """Every entry the request names is judged again from current state; no other entry or field may change."""
        requested = {}
        for entry_id, _ in self.update_items(plan.arguments):
            requested[entry_id] = self.find(ctx, table["id"], entry_id, uow)
        for current in requested.values():
            if current["removed"]:
                raise DomainError("validation_error", "This entry was removed; add the record again to restore it")
            self.crm.access.require(ctx, self.reference(current), uow)
        changed = [change for change in table_changes if change.resource.resource_type == "crm_entries"]
        if (len({change.resource.resource_id for change in changed}) != len(changed)
                or any(change.action != "update" or change.resource.resource_id not in requested
                       or set(change.values) - UPDATE_FIELDS for change in changed)):
            raise DomainError("invalid_plan", "The change must only edit the entries the request names")

    def validate_replay(self, operation, ctx, args, replay, uow):
        """A saved receipt is returned only while the caller may still have what it describes.

        The result of an add or update shows the entry's Task: its Table, its composed effects and that Task's read
        policy in its own scope are decided again, a created Task included. A removal's result names no Task data."""
        if replay.status != "ok":
            return  # a staged proposal is only a reference
        ctx = table_context(ctx, args, uow)
        self.crm.recheck(ctx, operation, table_namespace(ctx, args), uow, *(("tasks_create",) if "create" in args else ()))
        table = self.table(ctx, args["table_id"], uow)
        if "updates" in args:
            # The compact receipt names no Task, so each requested entry's Task is judged again from the request itself.
            for entry_id, _ in self.update_items(args):
                self.crm.access.require(ctx, self.reference(self.find(ctx, table["id"], entry_id, uow)), uow)
            return
        entry = (plain(replay.data) or {}).get("entry")
        if entry is not None and not entry.get("unavailable"):
            self.crm.access.require(ctx, entry["record_ref"], uow)

    def apply(self, ctx, plan, uow):
        ctx = table_context(ctx, plan.arguments, uow)
        args, operation = plan.arguments, plan.operation_name
        if "updates" in args:
            return self.apply_updates(ctx, plan, uow)
        table_changes, task_changes = self.split(plan)
        entry_change = next((c for c in table_changes if c.resource.resource_type == "crm_entries"), None)
        before = uow.repository("crm_entries", business_id=ctx.business_id).get(entry_change.resource.resource_id) if (
            entry_change and entry_change.action == "update") else None
        created = bool(task_changes)
        if created:
            # The Task service's own apply: its writes, follows, system entry and event, inside this one transaction.
            self.crm.tasks.apply(ctx, replace(plan, operation_name="tasks_create", arguments=self.task_arguments(ctx, args),
                                              changes=task_changes), uow)
        table_plan = replace(plan, changes=table_changes)
        applied = apply_records(ctx, replace(table_plan, changes=tuple(c for c in table_changes if c.action != "capture_table_version")), uow)
        captured = self.crm.tables.capture_all(ctx, table_plan, applied, uow)
        if entry_change is not None:
            after = uow.repository("crm_entries", business_id=ctx.business_id).get(entry_change.resource.resource_id)
            self.crm.threads.record_membership(ctx, uow, before, after)
        table = captured[0][0] if captured else self.crm.tables.table(ctx, args["table_id"], uow)
        # Like every Table write, a changing batch also names the job that publishes its revision.
        result = {"table": self.crm.tables.view(ctx, table, uow), **({"job_id": captured[0][1]["id"]} if captured else {})}
        if operation == "crm_entries_remove":
            return {**result, "entry_id": args["entry_id"], "removed": True}
        entry_id = entry_change.resource.resource_id if entry_change else (
            args["entry_id"] if "entry_id" in args else self.lookup(ctx, table["id"], args["record"], uow)["id"])
        entry = uow.repository("crm_entries", business_id=ctx.business_id).get(entry_id)
        view = self.view(ctx, table, entry, uow, ScopeCache())
        return {**result, "entry": view, **({"created_record": created} if operation == "crm_entries_add" else {})}

    def apply_updates(self, ctx, plan, uow):
        """Several entries of one Table in this transaction: one revision, one snapshot and job, one development per changed entry."""
        table_changes, _ = self.split(plan)
        entries = uow.repository("crm_entries", business_id=ctx.business_id)
        changed = [change.resource.resource_id for change in table_changes
                   if change.resource.resource_type == "crm_entries" and change.action == "update"]
        before = {identity: entries.get(identity) for identity in changed}
        table_plan = replace(plan, changes=table_changes)
        applied = apply_records(ctx, replace(table_plan, changes=tuple(c for c in table_changes if c.action != "capture_table_version")), uow)
        captured = self.crm.tables.capture_all(ctx, table_plan, applied, uow)
        for identity in changed:
            self.crm.threads.record_membership(ctx, uow, before[identity], entries.get(identity))
        table = captured[0][0] if captured else self.crm.tables.table(ctx, plan.arguments["table_id"], uow)
        # Compact on purpose: no Task record, no table view; the entries' own revisions say what moved.
        return {"table_id": table["id"], "table_revision": table["revision"],
                "entries": [{"entry_id": entry_id, "revision": entries.get(entry_id)["revision"], "changed": entry_id in changed}
                            for entry_id, _ in self.update_items(plan.arguments)],
                **({"job_id": captured[0][1]["id"]} if captured else {})}
