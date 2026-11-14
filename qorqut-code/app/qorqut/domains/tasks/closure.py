"""What a Task write does to the records that point at the Task.

A Todo entry, a Focus card and a follow-up reminder each name a Task and are written separately, so each used to
drift from the work it was about until somebody remembered to fix it. This is the host's ``TaskOperations.on_change``
hook, run inside every Task write:

* the Todo boards follow the Task's recorded workers and state (``Attention.follow_task``);
* the acting agent's pending inbox deliveries about the Task's earlier states are acknowledged, since it has just
  written the Task from its current state (deliveries that carry a message stay until the agent acknowledges them);
* when the stored state moves into done or cancelled, the Task's Focus card is archived and the reminders agents set
  for agents about it are cancelled.

Nothing here decides anything about the Task. A reopened Task returns to its workers' boards.
"""

from __future__ import annotations

from qorqut_operations.application.plans import timestamp, utc_clock
from qorqut_operations.tasks.schema import CLOSED_STATES

SEEN = "Seen: the agent wrote this Task from its current state"


def acknowledge_seen(uow, before, actor_id, clock=utc_clock):
    """Acknowledge the actor's pending deliveries about states of the Task it has now seen; returns how many.

    A Task write names the revision it was made from, so every earlier event about the Task's state or details describes
    something the writer has read. An event that carries a comment is left for the agent, because writing a Task does
    not show that a message on it was read, and so is an event claimed by a delegated assignment, which closes it itself."""
    if before is None:
        return 0
    row = before["row"]
    cursor = uow.connection.execute(
        "UPDATE inbox_deliveries SET acknowledged_at=?,outcome=? WHERE agent_id=? AND acknowledged_at IS NULL AND event_id IN ("
        "SELECT e.id FROM inbox_events e WHERE e.source_type='tasks' AND e.source_id=? AND e.business_id IS ? "
        "AND e.source_revision<=? AND e.event_type NOT LIKE '%note_added%' "
        "AND NOT EXISTS (SELECT 1 FROM operational_event_claims c WHERE c.event_id=e.id AND c.state='claimed'))",
        (timestamp(clock), SEEN, actor_id, row["id"], row["business_id"], row["revision"]))
    return cursor.rowcount


class TaskClosure:
    def __init__(self, attention, focus_board, reminders):
        self.attention, self.focus_board, self.reminders = attention, focus_board, reminders

    def __call__(self, ctx, uow, before, after):
        row = after["row"]
        result = {"todo": self.attention.follow_task(uow, before, after, ctx.principal_id),
                  "acknowledged": acknowledge_seen(uow, before, ctx.principal_id, self.attention.clock)
                  if ctx.principal_kind == "agent" else 0}
        if row["status"] in CLOSED_STATES and (before is None or before["row"]["status"] not in CLOSED_STATES):
            business_id, task_id = row["business_id"], row["id"]
            result.update(focus_card=self.focus_board.release_task(business_id, task_id, uow),
                          reminders=self.reminders.release_task(uow, business_id, task_id))
        return result


__all__ = ["SEEN", "TaskClosure", "acknowledge_seen"]
