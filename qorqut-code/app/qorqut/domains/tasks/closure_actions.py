"""The Action of a closed Task: recorded by the service for a worker, and reported to the coordinator.

Actions are the ledger of completed work, which the owner reads as a timeline. Only the coordinator and the learning
applier hold ``actions_record``; a worker finishes by closing its Task. A worker's result must not depend on somebody
recording it afterwards, so ``ClosureActions`` records it inside the Task write (``TaskClosure`` calls it):

* when a Task enters done from a state that is not closed, written by an agent whose role cannot record Actions itself.
  A cancelled Task, an edit of a closed Task, a Task closed along with its parent, and a closure by the coordinator, the
  learning applier or a person record nothing;
* as one automatic Action stored in Global and credited to the closing agent: ``business-operational`` and linked to the
  Task's project, or ``global-operational`` for a Global Task, with the Task as its ``task`` reference. Its text is the
  result this write set, or the Task's title, cut to the 20 words of generated Action text; the full result stays on
  the Task;
* once per Task: the Action's id is derived from the Task, so a Task reopened and closed again keeps the Action of its
  first closure, and nothing is recorded while the closer already has a current Action that references the Task.

The revision is admitted through the composition's ``on_revision`` like any other Action. Its announcement reaches
nobody: a new Action's only follower is its recorder, who is the acting agent. A failure to record never fails the Task
write: the attempt runs in a savepoint, is rolled back alone and logged.

``coordinator_hint`` is the read side: what the configured coordinator is told about a done Task he reads or closes, so
a result is recorded once and not twice.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging

from qorqut_operations import local_time
from qorqut_operations.actions import models
from qorqut_operations.actions.writer import new_action, settle
from qorqut_operations.application.plans import PlanBuilder, apply_records, utc_clock
from qorqut_operations.global_agents import global_profile
from qorqut_operations.roles import LEARNING_APPLIER_ROLE, ORDUBASHY_ROLE
from qorqut_operations.tasks.schema import CLOSED_STATES
from qorqut_operations.tasks.types import KEY
from qorqut.management.identity import configured_ordubashy
from qorqut.reminders.service import savepoint

logger = logging.getLogger(__name__)

# The roles that hold ``actions_record``: their closures are theirs to record, or not.
RECORDING_ROLES = frozenset({ORDUBASHY_ROLE, LEARNING_APPLIER_ROLE})
PROVENANCE = "task_closure"
# How long after a closure the coordinator is still told that its result has no Action.
UNRECORDED_WINDOW = timedelta(hours=48)
# Both lookups scan Actions by their ``task`` reference, which has no index: the cheap column comes first, and a project
# Task reads only the Actions linked to its project.
BY_CLOSER = ("SELECT 1 FROM action_logs WHERE withdrawn=0 AND performed_by=? AND json_extract(refs_json,'$.task')=? "
             "LIMIT 1")
RECORDED = ("SELECT a.action_key, a.action_text, COALESCE(p.display_name, r.display_name) AS performer FROM {source} "
            "LEFT JOIN principals p ON p.id=a.performed_by LEFT JOIN principals r ON r.id=a.recorded_by "
            "WHERE {scope} AND a.withdrawn=0 AND json_extract(a.refs_json,'$.task')=? "
            "ORDER BY a.created_at, a.action_key LIMIT 1")
IN_PROJECT = RECORDED.format(source="action_businesses m JOIN action_logs a ON a.action_key=m.action_key",
                             scope="m.business_id=?")
IN_GLOBAL = RECORDED.format(source="action_logs a", scope="a.category='global-operational'")


def closure_identity(business_id, task_id) -> str:
    """The id of the one Action the service records for a Task's closure."""
    return models.source_identity(None, f"task-closure:{business_id or 'global'}:{task_id}")


def status_line(state) -> str:
    """The result a Task state holds (its status line); the raw row keeps its fields as JSON text."""
    fields = state["row"].get("fields_json") if state else None
    if isinstance(fields, str):
        fields = json.loads(fields or "{}")
    typed = (fields or {}).get(KEY)
    line = typed.get("status_line") if isinstance(typed, dict) else None
    return line.strip() if isinstance(line, str) else ""


def recorded_action(uow, business_id, task_id):
    """The first current Action that references the Task from the Task's own scope, or None: one linked to the Task's
    project, or a ``global-operational`` one for a Global Task."""
    row = (uow.connection.execute(IN_GLOBAL, (task_id,)) if business_id is None
           else uow.connection.execute(IN_PROJECT, (business_id, task_id))).fetchone()
    return dict(row) if row else None


class ClosureActions:
    """``on_revision`` is the composition's one Action revision admission; ``clock`` times the Action."""

    def __init__(self, on_revision, *, clock=utc_clock):
        self.on_revision, self.clock = on_revision, clock

    def record(self, ctx, uow, before, after):
        """Record the closure ``before`` -> ``after`` when it is a worker's result; returns the Action's key, or None.

        Never raises for a failed record: the Task write this runs in must commit whatever happens here."""
        if (ctx.principal_kind != "agent" or after["row"]["status"] != "done"
                or (before is not None and before["row"]["status"] in CLOSED_STATES)):
            return None
        try:
            with savepoint(uow, "task_closure_action"):
                return self.write(ctx, uow, before, after)
        except Exception:
            logger.exception("The Action of closed Task %s was not recorded; the Task write continues", after["row"]["id"])
            return None

    def write(self, ctx, uow, before, after):
        row = after["row"]
        business_id, task_id, closer = row["business_id"], row["id"], ctx.principal_id
        profile = global_profile(uow.connection, closer)
        if profile is not None and profile["profile"]["role"]["id"] in RECORDING_ROLES:
            return None
        identity = closure_identity(business_id, task_id)
        if (uow.repository("action_logs", business_id=None).get(identity) is not None
                or uow.connection.execute(BY_CLOSER, (closer, task_id)).fetchone() is not None):
            return None
        result = status_line(after)
        text = result if result and result != status_line(before) else row["title"]
        # Stored in Global, as the coordinator's own records of project work are: the project is a link, not the owner.
        scope = replace(ctx, business_id=None, review_business_id=None)
        builder = PlanBuilder(scope, "actions_record", {"task_id": task_id}, "Record the closed Task's result", clock=self.clock)
        project = business_id is not None
        action = new_action(builder, uow, text=models.concise(text), origin="automatic",
                            category="business-operational" if project else "global-operational",
                            businesses=[business_id] if project else [], refs={"task": task_id},
                            occurrence=local_time.recorded_time(builder.at), identity=identity,
                            provenance={"via": PROVENANCE})
        plan = builder.build()
        apply_records(scope, plan, uow)
        settle(scope, plan, uow, on_revision=self.on_revision)
        return action["action_key"]


def closed_within(closed_at, now, window) -> bool:
    try:
        at = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    return now - (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) <= window


def coordinator_hint(runtime, ctx, task, *, clock=utc_clock):
    """What the configured coordinator is told about the Action of a done Task he read or wrote, or None.

    ``task`` names the Task as stored (``id`` and ``business_id``). An Action that references it from its own scope is
    named, so it is not recorded again; without one, a Task closed within ``UNRECORDED_WINDOW`` is said to have none.
    Anyone else, and any Task that is not done, gets nothing. Read-only, after the call's own work."""
    if ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None:
        return None
    business_id, task_id = task["business_id"], task["id"]
    with runtime.application.database.read() as uow:
        if configured_ordubashy(uow) != ctx.principal_id:
            return None
        row = uow.connection.execute("SELECT status, latest_completed_at FROM tasks WHERE business_id IS ? AND id=?",
                                     (business_id, task_id)).fetchone()
        if row is None or row["status"] != "done":
            return None
        action = recorded_action(uow, business_id, task_id)
        if action is None and not closed_within(row["latest_completed_at"], clock(), UNRECORDED_WINDOW):
            return None
    instructions = runtime.resolver.require_agentic_view().runtime_instructions()
    if action is None:
        return instructions.render("task-action-missing")
    return instructions.render("task-action-recorded", ACTION_ID=action["action_key"],
                               PERFORMER=action["performer"] or "an unnamed performer",
                               TEXT=models.one_line(action["action_text"]))


__all__ = ["RECORDING_ROLES", "UNRECORDED_WINDOW", "ClosureActions", "closure_identity", "coordinator_hint",
           "recorded_action"]
