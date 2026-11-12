"""What closing a Task does to the records that point at it.

A Todo entry, a Focus card and a follow-up reminder each name a Task and are written separately, so each used to
outlive the work it was about until somebody remembered to remove it. This is the host's ``TaskOperations.on_change``
hook: when a Task's stored state moves into done or cancelled, the same transaction takes the Task off every Todo
board (clearing a focus on it), archives its Focus card and cancels the reminders agents set for agents about it.

Nothing here decides anything about the Task; a reopened Task gets none of it back, and is put on a board or on
Focus again the ordinary way.
"""

from __future__ import annotations

from qorqut_operations.tasks.schema import CLOSED_STATES


class TaskClosure:
    def __init__(self, attention, focus_board, reminders):
        self.attention, self.focus_board, self.reminders = attention, focus_board, reminders

    def __call__(self, ctx, uow, before, after):
        row = after["row"]
        if row["status"] not in CLOSED_STATES or (before is not None and before["row"]["status"] in CLOSED_STATES):
            return None
        business_id, task_id = row["business_id"], row["id"]
        return {"todo_entries": self.attention.release_task(uow, business_id, task_id, row["created_at"], ctx.principal_id),
                "focus_card": self.focus_board.release_task(business_id, task_id, uow),
                "reminders": self.reminders.release_task(uow, business_id, task_id)}


__all__ = ["TaskClosure"]
