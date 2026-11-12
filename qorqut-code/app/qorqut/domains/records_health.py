"""Read-only counts of where other operational records disagree with the Task record.

The Task holds the state of a piece of work. Todo boards, reminders, Focus cards and CRM rows point at Tasks and
are written separately, so each can drift. Every check here names one such drift, counts it and lists a few
examples. Nothing is changed.

The stored Task status is read, never the Signoff projection: that projection shows a pending question as
``needs_input`` even on a closed Task, which would hide exactly the closed Tasks this report looks for.

``records_reconcile`` is the one-time alignment for records written before the boards and Focus cards followed the
Task. It makes the Task say what the older records say (a board's agent becomes a recorded worker, a Task card's
text becomes the Task's status line) and removes board entries for Tasks that are closed or archived. The report
lists exactly what it would do under ``repairs``, so reading the report is the dry run. Every change is made through
the Task engine and the board's own removal, in one transaction.
"""

from datetime import timedelta
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.types import IDENTIFIER, LINE_LIMIT

from qorqut.management.identity import configured_ordubashy, installation_owner

EXAMPLES = 5
REPAIR_EXAMPLES = 20
RECONCILED = "Reconciled: the Task was already on this agent's Todo board"
RECONCILE_DESCRIPTION = ("Owner and coordinator, once after an upgrade. Applies the repairs that records_health lists: records "
                         "each Todo board's agent as a worker of the open Tasks on its board, removes Todo entries for closed "
                         "or archived Tasks, and moves each Task card's text to the Task's status line. Read records_health "
                         "first; it shows exactly what this will change.")
WORKING_STALE = timedelta(days=1)
PARKED_STALE = timedelta(days=7)
DESCRIPTION = ("Owner and coordinator read. Counts where Todo boards, reminders, Focus cards and CRM rows disagree "
               "with the Tasks they point at, with a few examples of each; summary lists the non-zero counts. Changes "
               "nothing.")

# A work Task is an unarchived Task of the plain type; Customers, Stories and Email Tasks are records of another kind.
WORK = "t.archived=0 AND coalesce(json_extract(t.fields_json,'$.\"$task\".task_type'),'task')='task'"
OPEN = "t.status NOT IN ('done','cancelled')"
CLOSED = "t.status IN ('done','cancelled')"
ARCHIVED_PROJECT = "EXISTS (SELECT 1 FROM businesses b WHERE b.id=t.business_id AND b.archived_at IS NOT NULL)"
# A board entry names one Task identity: its namespace, id and creation time.
ENTRY = ("attention_memberships m JOIN tasks t ON t.business_id IS m.task_business_id AND t.id=m.task_id "
         "AND t.created_at=m.task_created_at")
HOLDS = "EXISTS (SELECT 1 FROM json_each(t.worker_ids_json) w WHERE w.value=m.agent_id)"
LINKED = ("JOIN tasks t ON t.id=json_extract({column},'$.id') "
          "AND t.business_id IS json_extract({column},'$.business_id')")
TASK = "t.business_id, t.id, t.title, t.status"

# name -> (what it means, FROM ... WHERE ..., extra example columns)
CHECKS = {
    "todo_on_closed_task": ("Todo entries whose Task is done or cancelled",
        f"{ENTRY} WHERE {CLOSED}", "m.agent_id"),
    "todo_on_archived_task": ("Todo entries whose Task or project is archived",
        f"{ENTRY} WHERE {OPEN} AND (t.archived=1 OR {ARCHIVED_PROJECT})", "m.agent_id"),
    "todo_holder_not_worker": ("Todo entries on an open Task whose board agent is not a recorded worker",
        f"{ENTRY} WHERE {OPEN} AND t.archived=0 AND NOT {HOLDS}", "m.agent_id"),
    "open_under_closed_parent": ("Open work Tasks whose parent is done or cancelled",
        f"tasks t JOIN tasks p ON p.business_id IS t.business_id AND p.id=t.parent_id "
        f"WHERE {WORK} AND {OPEN} AND p.status IN ('done','cancelled')", None),
    "open_in_archived_project": ("Open work Tasks in an archived project",
        f"tasks t WHERE {WORK} AND {OPEN} AND {ARCHIVED_PROJECT}", None),
    "open_without_worker": ("Open work Tasks with no recorded worker",
        f"tasks t WHERE {WORK} AND {OPEN} AND json_array_length(t.worker_ids_json)=0", None),
    "working_untouched": ("Work Tasks marked working and not written for over a day",
        f"tasks t WHERE {WORK} AND t.status='working' AND t.updated_at<:working_before", None),
    "parked_without_wake_time": ("Work Tasks waiting or blocked with neither a snooze nor a due date",
        f"tasks t WHERE {WORK} AND t.status IN ('waiting','blocked') AND t.snoozed_until IS NULL AND t.due IS NULL", None),
    "parked_untouched": ("Work Tasks waiting or blocked and not written for over a week",
        f"tasks t WHERE {WORK} AND t.status IN ('waiting','blocked') AND t.updated_at<:parked_before", None),
    "reminder_on_closed_task": ("Live reminders linked to a done or cancelled Task",
        f"reminders r {LINKED.format(column='r.linked_item_json')} "
        f"WHERE r.state IN ('scheduled','due') AND json_extract(r.linked_item_json,'$.type')='task' AND {CLOSED}",
        "r.id AS reminder_id"),
    "focus_card_on_closed_task": ("Focus cards still shown for a done or cancelled Task",
        f"focus_cards f {LINKED.format(column='f.source_json')} "
        f"WHERE f.archived=0 AND json_extract(f.source_json,'$.type')='task' AND {CLOSED}", "f.id AS card_id"),
    "crm_record_typed_as_work": ("CRM row records stored as open work Tasks",
        f"crm_entries e JOIN tasks t ON t.business_id IS e.record_business_id AND t.id=e.record_task_id "
        f"WHERE e.removed=0 AND {WORK} AND {OPEN}", "e.table_id"),
}
TOTALS = {
    "open_work_tasks": f"tasks t WHERE {WORK} AND {OPEN}",
    "todo_entries": "attention_memberships m",
}


def disagreements(connection, now):
    """Every check as ``{count, meaning, examples}`` plus the totals that give the counts their scale."""
    moment = {"working_before": timestamp(lambda: now - WORKING_STALE), "parked_before": timestamp(lambda: now - PARKED_STALE)}

    def bound(sql):
        return {name: value for name, value in moment.items() if ":" + name in sql}

    checks = {}
    for name, (meaning, body, extra) in CHECKS.items():
        count = connection.execute(f"SELECT COUNT(*) FROM {body}", bound(body)).fetchone()[0]
        columns = TASK + (", " + extra if extra else "")
        rows = connection.execute(f"SELECT {columns} FROM {body} ORDER BY t.updated_at DESC, t.id LIMIT {EXAMPLES}",
                                  bound(body)).fetchall() if count else []
        names = ["business_id", "task_id", "title", "status"] + ([extra.split(" AS ")[-1].split(".")[-1]] if extra else [])
        checks[name] = {"count": count, "meaning": meaning, "examples": [dict(zip(names, row)) for row in rows]}
    totals = {name: connection.execute(f"SELECT COUNT(*) FROM {body}").fetchone()[0] for name, body in TOTALS.items()}
    report = {"as_of": timestamp(lambda: now), "totals": totals, "checks": checks,
              "disagreements": sum(item["count"] for item in checks.values())}
    return {**report, "summary": summary(report)}


def repairs(connection):
    """What ``records_reconcile`` would change, as full lists. Nothing is written.

    ``record_worker``: a board entry on an open Task whose board agent is not a recorded worker. ``remove_entry``: a
    board entry on a Task that is closed, archived or in an archived project. ``set_status_line``: a shown Task card
    with text, on a Task that has no status line yet. ``status_line_needs_rewrite``: the same, where the text is too
    long or names a record identifier, so somebody has to write the line."""
    listed = lambda sql, names: [dict(zip(names, row)) for row in connection.execute(sql)]
    entry = ("membership_id", "agent_id", "business_id", "task_id", "title")
    found = {
        "record_worker": listed(f"SELECT m.id, m.agent_id, t.business_id, t.id, t.title FROM {ENTRY} WHERE {OPEN} AND t.archived=0 "
                                f"AND NOT {ARCHIVED_PROJECT} AND NOT {HOLDS} ORDER BY t.business_id, t.id, m.agent_id", entry),
        "remove_entry": listed(f"SELECT m.id, m.agent_id, t.business_id, t.id, t.title FROM {ENTRY} WHERE {CLOSED} OR t.archived=1 "
                               f"OR {ARCHIVED_PROJECT} ORDER BY t.business_id, t.id, m.agent_id", entry),
        "set_status_line": [], "status_line_needs_rewrite": []}
    cards = connection.execute(
        f"SELECT f.id, f.reason, t.business_id, t.id, t.title, t.fields_json FROM focus_cards f "
        f"{LINKED.format(column='f.source_json')} WHERE f.archived=0 AND json_extract(f.source_json,'$.type')='task' "
        "AND coalesce(f.reason,'')<>'' ORDER BY f.position, f.id")
    for card_id, reason, business_id, task_id, title, fields in cards:
        if (json.loads(fields or "{}").get("$task") or {}).get("status_line"):
            continue
        text = " ".join(reason.split())
        fits = len(text) <= LINE_LIMIT and not IDENTIFIER.search(text)
        found["set_status_line" if fits else "status_line_needs_rewrite"].append(
            {"card_id": card_id, "business_id": business_id, "task_id": task_id, "title": title, "text": text})
    return found


def repair_summary(found):
    return {name: {"count": len(items), "examples": items[:REPAIR_EXAMPLES]} for name, items in found.items()}


def summary(report):
    """One line per non-zero check, so a reader sees what to fix before the examples of each."""
    return [f"{item['count']} — {item['meaning']}" for item in report["checks"].values() if item["count"]]


class RecordsHealth:
    def __init__(self, *, clock=utc_clock):
        self.clock = clock
        # The Todo boards and the Focus board, bound by the runtime once they exist; reconciling needs both.
        self.attention = self.focus_board = None

    def bind(self, attention, focus_board):
        self.attention, self.focus_board = attention, focus_board
        return self

    def register(self, registry):
        registry.register(OperationDescriptor("records_health", schema({"scope": {"const": "global"}}),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", effect_kind="read",
            supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}), description=DESCRIPTION,
            resolve_namespace=lambda ctx, args: "global", read=self.read))
        registry.register(OperationDescriptor("records_reconcile", schema({"scope": {"const": "global"}}),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", supports_signoff=False,
            exposure=frozenset({"mcp", "dashboard"}), description=RECONCILE_DESCRIPTION,
            resolve_namespace=lambda ctx, args: "global", prepare=self.prepare, apply=self.apply))
        return self

    @staticmethod
    def allowed(ctx, uow):
        if ctx.principal_id not in {installation_owner(uow), configured_ordubashy(uow)} - {None}:
            raise DomainError("forbidden", "Record health is read and reconciled by the owner or the coordinator")

    def read(self, ctx, args, options, uow):
        self.allowed(ctx, uow)
        return {**disagreements(uow.connection, self.clock()), "repairs": repair_summary(repairs(uow.connection))}

    def prepare(self, ctx, args, options, uow):
        self.allowed(ctx, uow)
        if self.attention is None or self.focus_board is None:
            raise DomainError("operation_unavailable", "Todo boards and Focus are not installed")
        return PlanBuilder(ctx, "records_reconcile", args, "Reconcile records", clock=self.clock, namespace="global",
                           affected_scope={"kind": "global"}).build()

    def apply(self, ctx, plan, uow):
        """Apply every listed repair as the caller. A repair the Task engine refuses is reported and the rest continue."""
        self.allowed(ctx, uow)
        from qorqut.agents.attention import task_workers
        found, skipped = repairs(uow.connection), []
        done = {"record_worker": 0, "remove_entry": 0, "set_status_line": 0}
        for item in found["record_worker"]:
            ref = {"type": "task", "id": item["task_id"], "business_id": item["business_id"]}
            try:
                task = self.attention.task(ctx, ref, uow, lifecycle_only=True)
                written = self.attention.write_task(ctx, uow, ref, task["revision"],
                                                    {"worker_ids": sorted(task_workers(task) | {item["agent_id"]})})
                # The Task was already on this agent's board, so being recorded as its worker is not news to act on.
                uow.connection.execute(
                    "UPDATE inbox_deliveries SET acknowledged_at=?,outcome=? WHERE agent_id=? AND acknowledged_at IS NULL "
                    "AND event_id IN (SELECT id FROM inbox_events WHERE source_type='tasks' AND source_id=? "
                    "AND business_id IS ? AND source_revision=?)",
                    (timestamp(self.clock), RECONCILED, item["agent_id"], ref["id"], ref["business_id"], written["revision"]))
                done["record_worker"] += 1
            except DomainError as error:
                skipped.append({**item, "repair": "record_worker", "code": error.code, "message": error.message})
        stale = [item["membership_id"] for item in found["remove_entry"]]
        if stale:
            rows = uow.connection.execute("SELECT * FROM attention_memberships WHERE id IN (" + ",".join("?" for _ in stale)
                                          + ") ORDER BY agent_id,id", stale).fetchall()
            done["remove_entry"] = self.attention.release(uow, rows, ctx.principal_id, "Task closed or archived")
        for item in found["set_status_line"]:
            source = {"type": "task", "id": item["task_id"], "business_id": item["business_id"]}
            try:
                self.focus_board.write_status_line(ctx, source, item["text"], uow)
            except DomainError as error:
                skipped.append({**item, "repair": "set_status_line", "code": error.code, "message": error.message})
                continue
            uow.connection.execute("UPDATE focus_cards SET reason='',revision=revision+1,updated_at=? WHERE id=?",
                                   (timestamp(self.clock), item["card_id"]))
            done["set_status_line"] += 1
        if done["set_status_line"]:
            uow.next_sequence("focus_board")
        return {"applied": done, "skipped": skipped, "status_line_needs_rewrite": found["status_line_needs_rewrite"],
                "health": disagreements(uow.connection, self.clock())}
