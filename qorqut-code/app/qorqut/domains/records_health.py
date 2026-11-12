"""Read-only counts of where other operational records disagree with the Task record.

The Task holds the state of a piece of work. Todo boards, reminders, Focus cards and CRM rows point at Tasks and
are written separately, so each can drift. Every check here names one such drift, counts it and lists a few
examples. Nothing is changed.

The stored Task status is read, never the Signoff projection: that projection shows a pending question as
``needs_input`` even on a closed Task, which would hide exactly the closed Tasks this report looks for.
"""

from datetime import timedelta

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError

from qorqut.management.identity import configured_ordubashy, installation_owner

EXAMPLES = 5
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


def summary(report):
    """One line per non-zero check, so a reader sees what to fix before the examples of each."""
    return [f"{item['count']} — {item['meaning']}" for item in report["checks"].values() if item["count"]]


class RecordsHealth:
    def __init__(self, *, clock=utc_clock):
        self.clock = clock

    def register(self, registry):
        registry.register(OperationDescriptor("records_health", schema({"scope": {"const": "global"}}),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", effect_kind="read",
            supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}), description=DESCRIPTION,
            resolve_namespace=lambda ctx, args: "global", read=self.read))
        return self

    def read(self, ctx, args, options, uow):
        if ctx.principal_id not in {installation_owner(uow), configured_ordubashy(uow)} - {None}:
            raise DomainError("forbidden", "Record health is read by the owner or the coordinator")
        return disagreements(uow.connection, self.clock())
