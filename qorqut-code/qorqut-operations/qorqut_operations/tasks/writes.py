"""Tasks written inside another domain's plan: the engine's one announcement still follows them.

A domain that records Task rows, edges and comments in its own prepared plan (handoff repair, for example) must still give
the Task its follows, its thread entry and its one event, so it applies such a plan through ``TaskWrites.apply`` instead
of calling ``apply_records`` directly. The Task flow itself (states, validation, discussion, routing) is the engine's
``TaskOperations``; this only reads the Task before and after the records are applied and hands the difference to
``TaskOperations.announce`` in the same transaction."""

from __future__ import annotations

from qorqut_operations.application.plans import apply_records
from qorqut_operations.contracts import plain
from qorqut_operations.tasks import events


def touched(plan) -> list[str]:
    """The Tasks a plan writes: recorded rows and new assignment or dependency edges, in plan order."""
    found = []
    for change in plan.changes:
        kind, values = change.resource.resource_type, plain(change.values)
        if kind == "tasks" and change.action != "policy_effect":
            identity = change.resource.resource_id
        elif kind == "relationships" and change.action == "insert" and values.get("from_type") == "task":
            identity = values["from_id"]
        else:
            continue
        if identity not in found:
            found.append(identity)
    return found


class TaskWrites:
    def __init__(self, tasks):
        self.tasks = tasks

    def apply(self, ctx, plan, uow):
        """Apply the plan's records, then announce every Task they touched; returns the applied records' result."""
        ids = touched(plan)
        before = {identity: events.state(uow, ctx.business_id, identity) for identity in ids}
        result = apply_records(ctx, plan, uow)
        for identity in ids:
            after = events.state(uow, ctx.business_id, identity)
            if after is None:
                continue
            comments = [plain(change.values) for change in plan.changes
                        if change.resource.resource_type == "thread_messages" and plain(change.values).get("item_type") == "task"
                        and plain(change.values).get("item_id") == identity]
            self.tasks.announce(ctx, uow, plan, before[identity], after, comments)
        return result


__all__ = ["TaskWrites", "touched"]
