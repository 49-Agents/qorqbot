"""Snooze expiry: one durable due event per Task and snooze generation.

A snooze only defers the Task's due reminder to ``snoozed_until``; it is not a state, a cancellation or a subscription
change, and it changes nothing about the Task when it expires. The backend reconciler finds snoozes that have passed,
records a wake for the Task's current generation in the same transaction as the event it emits, and so a repeated tick,
a restart or two racing ticks cannot notify twice for the same due state. A Task that is done or cancelled gets no
reminder.

Responsibility is computed once: all of the Task's actual assignees, or its creator when it has none. Each responsible
principal is classified from the principal registry. Agents receive the event through the inbox (the event writer
keeps only the eligible ones). Human responsibility is not an inbox row: it always goes through the typed host
``Routing.snooze_routes(uow, business_id, task, human_ids)``, creator fallback included, and the host answers with the
agents to tell on their behalf. The creator is never told while somebody else is assigned. A wake nobody is eligible
for is still recorded, so it is never retried."""

from __future__ import annotations

from qorqut_operations.application.plans import timestamp, utc_clock
from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.tasks.events import SOURCE_TYPE, assigned


def responsible(uow, task) -> list[str]:
    """The principals a snooze wake is for: every actual assignee, otherwise the creator."""
    return assigned(uow, task["business_id"], task["id"]) or [task["created_by"]]


def recipients(uow, routing, task) -> list[str]:
    """The required recipients of a snooze wake for ``task``."""
    identities = sorted(set(responsible(uow, task)))
    kinds = {row[0]: row[1] for row in uow.connection.execute(
        f"SELECT id, kind FROM principals WHERE id IN ({','.join('?' for _ in identities)})", identities)}
    required = {identity for identity in identities if kinds.get(identity) == "agent"}
    humans = [identity for identity in identities if kinds.get(identity) == "human"]
    if humans:
        required |= set(routing.snooze_routes(uow, task["business_id"], task, humans))
    return sorted(required)


def reconcile_snoozes(uow, routing, *, now: str | None = None, limit: int = 500) -> list[dict]:
    """Emit ``snooze_expired`` once for every passed snooze generation; returns what was woken.

    The caller opens one write unit of work (the backend lifecycle owns scheduling); nothing here calls a provider."""
    uow.require_write()
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise DomainError("validation_error", "The snooze batch size is between 1 and 5000")
    now = now or timestamp(utc_clock)
    due = uow.connection.execute(
        "SELECT * FROM tasks t WHERE t.snoozed_until IS NOT NULL AND t.snoozed_until<=? AND t.status NOT IN ('done','cancelled') "
        "AND NOT EXISTS (SELECT 1 FROM task_snooze_wakes w WHERE w.business_id IS t.business_id AND w.task_id=t.id "
        "AND w.snooze_generation=t.snooze_generation) ORDER BY t.snoozed_until, t.business_id, t.id LIMIT ?", (now, limit)).fetchall()
    woken = []
    for row in due:
        task = dict(row)
        event_id = emit_event(uow, business_id=task["business_id"], source_type=SOURCE_TYPE, source_id=task["id"],
                              source_revision=task["revision"], actor_id=None, event_type="snooze_expired",
                              summary=task["title"], required=recipients(uow, routing, task))
        uow.connection.execute(
            "INSERT INTO task_snooze_wakes(business_id,task_id,snooze_generation,snoozed_until,event_id,created_at) VALUES(?,?,?,?,?,?)",
            (task["business_id"], task["id"], task["snooze_generation"], task["snoozed_until"], event_id, utc_now()))
        woken.append({"business_id": task["business_id"], "task_id": task["id"], "snoozed_until": task["snoozed_until"],
                      "snooze_generation": task["snooze_generation"], "event_id": event_id})
    return woken


__all__ = ["reconcile_snoozes", "recipients", "responsible"]
