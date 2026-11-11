"""Stale work: one inbox event when a Task has sat untouched for longer than its state allows.

``STALE_AFTER`` is the one rule: the holders view marks a Task with it on read, and the backend reconciler pushes with
it. A Task is untouched while its ``updated_at`` does not move, so any write to it (a state or Worker change, a note)
ends the stretch. The reconciler finds Tasks past the limit, asks the host's ``Oversight`` port who answers for each
and emits one ``stale`` event for the ones somebody answers for; a Task nobody answers for is left alone.

The emitted events are the only memory: a Task is told again only after a further full period with no change, so a
notice that was acknowledged while nothing happened comes back, and a repeated tick, a restart or two racing ticks
cannot notify twice within a period. Nothing about the Task changes and no thread entry is written. A snoozed Task,
a Customer record, an archived Task and a Task of an archived project are never stale."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.tasks.events import SOURCE_TYPE

# How long a Task may sit untouched in a state before it is stale. Open work has not started and needs-input work
# waits on the owner, so neither goes stale.
STALE_AFTER = {"working": timedelta(days=1), "blocked": timedelta(days=3), "waiting": timedelta(days=7)}
EVENT_TYPE = "stale"
# Stale Tasks examined in one pass, oldest change first; the ones nobody answers for are examined again every pass.
SCAN = 2000
COLUMNS = "t.id,t.business_id,t.revision,t.title,t.status,t.updated_at,t.worker_ids_json,t.created_by"
NOT_CUSTOMER = "COALESCE(json_extract(t.fields_json,'$.\"$task\".task_type'),'task')!='customer'"


def stamp(value: datetime) -> str:
    """An instant in the stored UTC form, so it compares with ``updated_at`` and ``created_at`` as text."""
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def limits(current: datetime) -> dict[str, str]:
    """Per state, the instant before which an untouched Task is stale."""
    return {status: stamp(current - age) for status, age in STALE_AFTER.items()}


def untouched(task, current: datetime) -> str | None:
    """A system line for a Task that is still untouched and past its limit, or None once it is not."""
    age = STALE_AFTER.get(task["status"])
    try:
        since = datetime.fromisoformat((task["updated_at"] or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if age is None or since.tzinfo is None or current - since < age:
        return None
    days = (current - since).days
    return f"No change for {days} day{'' if days == 1 else 's'} while {task['status']}."


def reconcile_stale(uow, oversight, *, now: datetime | None = None, limit: int = 20) -> list[dict]:
    """Emit ``stale`` for Tasks untouched past their state's limit and not told within it; returns what was told.

    The caller opens one write unit of work (the backend lifecycle owns scheduling). At most ``limit`` events are
    emitted per call, the longest untouched first; nothing is written when nothing is due."""
    uow.require_write()
    if type(limit) is not int or not 1 <= limit <= 500:
        raise DomainError("validation_error", "The stale batch size is between 1 and 500")
    current = now or utc_clock()
    bounds, instant = limits(current), stamp(current)
    told = {(row[0], row[1]): row[2] for row in uow.connection.execute(
        "SELECT business_id,source_id,MAX(created_at) FROM inbox_events WHERE source_type=? AND event_type=? AND created_at>=? "
        "GROUP BY business_id,source_id", (SOURCE_TYPE, EVENT_TYPE, min(bounds.values())))}
    active = active_business_ids(uow.connection)
    due = []
    for status, bound in bounds.items():
        for row in uow.connection.execute(
                f"SELECT {COLUMNS} FROM tasks t WHERE t.archived=0 AND t.status=? AND t.updated_at<? "
                f"AND (t.snoozed_until IS NULL OR t.snoozed_until<=?) AND {NOT_CUSTOMER} ORDER BY t.updated_at LIMIT ?",
                (status, bound, instant, SCAN)):
            if (row["business_id"] is None or row["business_id"] in active) \
                    and told.get((row["business_id"], row["id"]), "") < bound:
                due.append(dict(row))
    due.sort(key=lambda task: (task["updated_at"], task["business_id"] or "", task["id"]))
    due = due[:SCAN]
    routes = oversight.stale_routes(uow, due) if due else {}
    pushed = []
    for task in due:
        if len(pushed) >= limit:
            break
        required = routes.get((task["business_id"], task["id"]))
        if not required:
            continue
        event_id = emit_event(uow, business_id=task["business_id"], source_type=SOURCE_TYPE, source_id=task["id"],
                              source_revision=task["revision"], actor_id=None, event_type=EVENT_TYPE,
                              summary=task["title"], required=required)
        if event_id is not None:
            pushed.append({"business_id": task["business_id"], "task_id": task["id"], "status": task["status"],
                           "updated_at": task["updated_at"], "event_id": event_id})
    return pushed


__all__ = ["EVENT_TYPE", "STALE_AFTER", "limits", "reconcile_stale", "stamp", "untouched"]
