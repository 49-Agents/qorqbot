"""The rules that keep a Task true as the one record of a piece of work, checked where the Task is written.

A Task that other records are computed from must not drift into a state nobody will come back to. Four things are
checked on a write, before anything is recorded:

* a work Task moved to ``waiting`` or ``blocked`` carries the time it comes back (``snoozed_until`` or ``due``), or,
  when blocked, the Task it is blocked by;
* a new or moved work Task does not go under a parent that is already closed;
* a Task is not closed over open work beneath it: the caller closes that work with it (``children: close``) or
  moves it first;
* a second open work Task with the same title under the same parent is refused unless the caller says it is separate.

The first, second and fourth, and the refusal in the third, apply to agents writing work Tasks (type ``task``). A person
deciding in the dashboard is not stopped, and Stories, Customers and Email Tasks are records of another kind.
``children: close`` works for every caller.

``stalled`` is read, never stored: a work Task marked working that nobody has written for a day.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from qorqut_operations.application.plans import rows
from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.schema import CLOSED_STATES
from qorqut_operations.tasks.types import attributes, state_key

PARKED = ("waiting", "blocked")
STALLED_AFTER = timedelta(days=1)
CASCADE_LIMIT = 200
# A list of targets (people to contact, places to apply) is rows of a table, not one work Task each.
CHILD_LIMIT = 10
OWNER_REQUESTED = {"type": "boolean", "description": (
    f"true only when the owner asked for more than {CHILD_LIMIT} separate open Tasks under this parent. Without it an agent "
    "cannot add one: a list of targets is kept as rows of a table (crm_entries_add or tables_*), one row each, and the work "
    "on the list is a single Task.")}
# Literal descriptions: a key in the tool-instruction catalog would have to be adopted by every installed catalog
# before the engine could start.
ALLOW_DUPLICATE = {"type": "boolean",
                   "description": "Create the Task although an open Task with the same title already exists under the same "
                                  "parent. Only when it is separate work; otherwise update the existing Task."}
CHILDREN = {"enum": ["close"],
            "description": "close: every open work Task under this one is closed with it, in the same state. Without it, "
                           "an agent cannot close a Task that still has open work beneath it."}
RESULT = {"type": "string", "minLength": 1, "maxLength": 1000,
          "description": "The outcome in one plain sentence for the owner, at most 240 characters. It becomes the Task's "
                         "status line and, when no note is given, the closing comment, so nothing else needs to repeat it."}
DELEGATE = ("Hand a Task to the agent who will do it, in one write: that agent becomes the Task's recorded worker and the "
            "brief is posted on the Task as the hand-over. The worker is notified. Use this instead of a separate message "
            "plus a board entry; the Task stays yours to answer for. For work of more than a couple of steps add plan, the "
            "ordered steps the worker follows without coming back to you, or template, a saved plan to copy; position "
            "places the Task in the worker's queue.")


def work(task) -> bool:
    return attributes(task)["task_type"] == "task"


def live(task) -> bool:
    return task["status"] not in CLOSED_STATES and not task.get("archived")


def stalled(task, now: str) -> bool:
    """A work Task marked working and not written for a day. ``now`` is the engine's UTC timestamp text."""
    if task["status"] != "working" or not work(task):
        return False
    cutoff = datetime.fromisoformat(now.replace("Z", "+00:00")) - STALLED_AFTER
    return datetime.fromisoformat(task["updated_at"].replace("Z", "+00:00")) < cutoff


def beneath(tasks, task_id) -> list[dict]:
    """Every open work Task anywhere under ``task_id``, nearest first."""
    children = {}
    for task in tasks:
        children.setdefault(task["parent_id"], []).append(task)
    found, queue, seen = [], [task_id], {task_id}
    while queue:
        for child in sorted(children.get(queue.pop(0), ()), key=lambda item: (item["created_at"], item["id"])):
            if child["id"] in seen:
                continue
            seen.add(child["id"])
            queue.append(child["id"])
            if live(child) and work(child):
                found.append(child)
    return found


def check(ctx, uow, arguments, before, current, *, blocked_by=()) -> list[dict]:
    """Refuse a write that breaks a rule above; returns the open work to close along with ``current`` (usually none)."""
    was, now = (before["status"] if before else None), current["status"]
    agent = ctx.principal_kind == "agent"
    parent_id = current.get("parent_id")
    if agent and work(current):
        if (now in PARKED and was != now and not current.get("snoozed_until") and not current.get("due")
                and not (now == "blocked" and blocked_by)):
            raise DomainError("validation_error",
                              f"A {now} Task needs the time you come back to it: set snoozed_until or due in the same write"
                              + (", or name the Task it waits on in blocked_by" if now == "blocked" else ""))
        if parent_id and now not in CLOSED_STATES and (before or {}).get("parent_id") != parent_id:
            parent = uow.repository("tasks", business_id=ctx.business_id).get(parent_id)
            if parent and parent["status"] in CLOSED_STATES:
                raise DomainError("validation_error", "The parent Task is closed. Reopen it, choose an open parent, or leave "
                                                      "this Task without a parent")
            if not arguments.get("owner_requested"):
                siblings = sum(1 for task in rows(uow, "tasks", ctx.business_id)
                               if task["parent_id"] == parent_id and task["id"] != current["id"] and live(task) and work(task))
                if siblings >= CHILD_LIMIT:
                    raise DomainError("validation_error",
                                      f"{siblings} open Tasks already sit under this parent. A list of targets belongs in a table, "
                                      "one row each (crm_entries_add or tables_*), with one Task for the work on the list. Pass "
                                      "owner_requested: true only if the owner asked for separate Tasks",
                                      details={"parent_id": parent_id, "open_children": siblings})
        if before is None and not arguments.get("allow_duplicate"):
            key = state_key(current["title"])
            twin = next((task for task in rows(uow, "tasks", ctx.business_id)
                         if task["id"] != current["id"] and task["parent_id"] == parent_id and live(task) and work(task)
                         and state_key(task["title"]) == key), None)
            if twin:
                raise DomainError("validation_error", "An open Task with this title already exists here. Update it, or pass "
                                                      "allow_duplicate: true if this is separate work",
                                  details={"existing_task_id": twin["id"]})
    if before is None or now not in CLOSED_STATES or was in CLOSED_STATES:
        return []
    cascade = arguments.get("children") == "close"
    if not cascade and not agent:
        return []
    tasks = rows(uow, "tasks", ctx.business_id)
    if cascade:
        found = beneath(tasks, current["id"])
        if len(found) > CASCADE_LIMIT:
            raise DomainError("validation_error", f"More than {CASCADE_LIMIT} open Tasks sit under this one; close them in parts")
        return found
    direct = [task for task in tasks if task["parent_id"] == current["id"] and live(task) and work(task)]
    if direct:
        raise DomainError("validation_error",
                          f"{len(direct)} open Task{'s' if len(direct) != 1 else ''} sit under this one. Close with "
                          "tasks_complete and children: close to close them with it, or move them to another parent first",
                          details={"open_child_ids": [task["id"] for task in direct[:20]]})
    return []


def closed_values(task, status, at) -> dict:
    """The recorded change of a Task closed along with its parent."""
    values = {"status": status, "updated_at": at}
    if status == "done":
        values["latest_completed_at"] = at
    return values


__all__ = ["ALLOW_DUPLICATE", "CHILDREN", "CHILD_LIMIT", "DELEGATE", "OWNER_REQUESTED", "RESULT", "beneath", "check", "closed_values", "stalled", "work"]
