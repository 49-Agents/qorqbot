"""What a Task write does to the records that point at the Task.

A Todo entry, a Focus card and a follow-up reminder each name a Task and are written separately, so each used to
drift from the work it was about until somebody remembered to fix it. This is the host's ``TaskOperations.on_change``
hook, run inside every Task write:

* the Todo boards follow the Task's recorded workers and state (``Attention.follow_task``);
* the acting agent's pending inbox deliveries about the Task's earlier states are acknowledged, since it has just
  written the Task from its current state (deliveries that carry a message or tell of a step of the Task's plan stay
  until the agent acknowledges them);
* when the stored state moves into done or cancelled, the Task's Focus card is archived and the reminders agents set
  for agents about it are cancelled;
* when the Task is closed or its plan is replaced or removed, the pending deliveries that say a step of that plan waits
  for confirmation are acknowledged for whoever holds them: nothing of that plan waits any more;
* when a worker closes the Task as done, the result is recorded as an Action (``closure_actions.ClosureActions``), so the
  owner's timeline of completed work does not wait for somebody to record it. A Task closed along with its parent
  (``carried``) records none: the parent's closure is the result.

Nothing here decides anything about the Task. A reopened Task returns to its workers' boards.
"""

from __future__ import annotations

import json

from qorqut_operations.application.plans import timestamp, utc_clock
from qorqut_operations.tasks.events import settle_submissions
from qorqut_operations.tasks.schema import CLOSED_STATES
from qorqut_operations.tasks.types import KEY

SEEN = "Seen: the agent wrote this Task from its current state"


def acknowledge_seen(uow, before, actor_id, clock=utc_clock):
    """Acknowledge the actor's pending deliveries about states of the Task it has now seen; returns how many.

    A Task write names the revision it was made from, so every earlier event about the Task's state or details describes
    something the writer has read. An event that carries a comment is left for the agent, because writing a Task does
    not show that a message on it was read, and so is an event claimed by a delegated assignment, which closes it itself.
    An event about a step of the Task's plan is left too: where the plan stands is not part of the Task's record, so a
    write of the Task does not show that a step handed in for confirmation, or the feedback it was sent back with, was read."""
    if before is None:
        return 0
    row = before["row"]
    cursor = uow.connection.execute(
        "UPDATE inbox_deliveries SET acknowledged_at=?,outcome=? WHERE agent_id=? AND acknowledged_at IS NULL AND event_id IN ("
        "SELECT e.id FROM inbox_events e WHERE e.source_type='tasks' AND e.source_id=? AND e.business_id IS ? "
        "AND e.source_revision<=? AND e.event_type NOT LIKE '%note_added%' AND e.event_type NOT LIKE '%plan_step_%' "
        "AND NOT EXISTS (SELECT 1 FROM operational_event_claims c WHERE c.event_id=e.id AND c.state='claimed'))",
        (timestamp(clock), SEEN, actor_id, row["id"], row["business_id"], row["revision"]))
    return cursor.rowcount


def stored_plan(state):
    """The plan a Task state holds; the raw row keeps its fields as JSON text."""
    fields = state["row"].get("fields_json") if state else None
    if isinstance(fields, str):
        fields = json.loads(fields or "{}")
    typed = (fields or {}).get(KEY)
    return (typed.get("plan") or []) if isinstance(typed, dict) else []


class TaskClosure:
    """``actions`` is the recorder of a worker's closure (``ClosureActions``); without one no Action is recorded."""

    def __init__(self, attention, focus_board, reminders, actions=None):
        self.attention, self.focus_board, self.reminders, self.actions = attention, focus_board, reminders, actions

    def __call__(self, ctx, uow, before, after, *, carried=False):
        """``carried`` marks a Task closed along with its parent (``children: close``) rather than by its own write."""
        row = after["row"]
        result = {"todo": self.attention.follow_task(uow, before, after, ctx.principal_id),
                  "acknowledged": acknowledge_seen(uow, before, ctx.principal_id, self.attention.clock)
                  if ctx.principal_kind == "agent" else 0}
        closed = row["status"] in CLOSED_STATES and (before is None or before["row"]["status"] not in CLOSED_STATES)
        if before is not None and stored_plan(before) and (closed or stored_plan(before) != stored_plan(after)):
            result["plan_submissions"] = settle_submissions(uow, row["business_id"], row["id"],
                                                            at=timestamp(self.attention.clock))
        if closed:
            business_id, task_id = row["business_id"], row["id"]
            result.update(focus_card=self.focus_board.release_task(business_id, task_id, uow),
                          reminders=self.reminders.release_task(uow, business_id, task_id))
            if self.actions is not None and not carried:
                # Last, and isolated: whatever happens to the Action, everything above stays part of the Task write.
                result["action"] = self.actions.record(ctx, uow, before, after)
        return result


__all__ = ["SEEN", "TaskClosure", "acknowledge_seen"]
