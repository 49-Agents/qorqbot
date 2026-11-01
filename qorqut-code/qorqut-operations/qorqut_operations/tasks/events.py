"""The one writer of Task inbox events and the thread entries that describe a change.

A Task event addresses two independent sets. *Required* recipients are a responsibility or a mandatory route: the final
assignees of a created, changed or reassigned Task, every assignment target (a removed assignee learns of it too), the
assignees of Tasks that depend on this one, and the routes the host makes mandatory (coordinator routing of owner work,
the Focus coordinator). *Optional* recipients are the Task's followers and the host's discretionary watchers. A follow
never removes a required delivery, and unfollowing never stops required ones; the acting principal is never woken by
its own change. All of it runs inside the writing transaction."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from qorqut_operations.discussion.events import emit_event
from qorqut_operations.discussion.follows import ThreadItem, followers
from qorqut_operations.discussion.threads import attach_event, system_entry

SOURCE_TYPE = "tasks"
# Row columns whose change is not, by itself, a change of the Task's details.
QUIET = frozenset({"revision", "updated_at", "status", "snooze_generation"})


def assigned(uow, business_id, task_id, *, agents_only: bool = False) -> list[str]:
    """The Task's assignees (``assigned_to`` edges), agents and people or agents only."""
    return [row[0] for row in uow.connection.execute(
        "SELECT to_id FROM relationships WHERE business_id IS ? AND from_type='task' AND from_id=? AND role='assigned_to'"
        + (" AND to_type='agent'" if agents_only else "") + " ORDER BY to_id", (business_id, task_id))]


def blockers(uow, business_id, task_id) -> list[str]:
    return [row[0] for row in uow.connection.execute(
        "SELECT DISTINCT to_id FROM relationships WHERE business_id IS ? AND from_type='task' AND from_id=? "
        "AND to_type='task' AND role='blocked_by' ORDER BY to_id", (business_id, task_id))]


def dependency_followers(uow, business_id, task_id) -> set[str]:
    """Agents responsible for Tasks that are blocked by this one (derived from the graph, never subscribed)."""
    return {row[0] for row in uow.connection.execute(
        "SELECT DISTINCT a.to_id FROM relationships d JOIN relationships a ON a.business_id IS d.business_id AND a.from_type='task' "
        "AND a.from_id=d.from_id AND a.role='assigned_to' AND a.to_type='agent' WHERE d.business_id IS ? "
        "AND d.from_type='task' AND d.to_type='task' AND d.role='blocked_by' AND d.to_id=?", (business_id, task_id))}


def routes(uow, routing, business_id, task: Mapping, actor_id, *, responsibility: bool, targets: Iterable[str] = ()):
    """``(required, optional)`` principal ids for an event about ``task``."""
    task_id = task["id"]
    required = set(targets) | dependency_followers(uow, business_id, task_id)
    if responsibility:
        required |= set(assigned(uow, business_id, task_id, agents_only=True))
    host_required, host_optional = routing.routes(uow, business_id, task, actor_id)
    required |= set(host_required)
    optional = set(followers(uow, ThreadItem(business_id, "task", task_id))) | set(host_optional)
    return required, optional


def announce(uow, routing, business_id, task: Mapping, actor_id, kinds, *, responsibility: bool, targets: Iterable[str] = (),
             summary=None, messages: Iterable[str] = ()) -> int | None:
    """Emit the one event for an operation on a Task and bind the thread entries written with it."""
    required, optional = routes(uow, routing, business_id, task, actor_id, responsibility=responsibility, targets=targets)
    event_id = emit_event(uow, business_id=business_id, source_type=SOURCE_TYPE, source_id=task["id"],
                          source_revision=task["revision"], actor_id=actor_id, event_type=",".join(kinds),
                          summary=summary or task["title"], required=required, optional=optional)
    if event_id is not None:
        attach_event(uow, ThreadItem(business_id, "task", task["id"]), messages, event_id)
    return event_id


def state(uow, business_id, task_id) -> dict | None:
    """Everything a change of the Task can alter, for comparing before and after one operation."""
    row = uow.connection.execute("SELECT * FROM tasks WHERE business_id IS ? AND id=?", (business_id, task_id)).fetchone()
    if row is None:
        return None
    labels = [label[0] for label in uow.connection.execute(
        "SELECT t.label_key FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.business_id IS ? AND i.item_type='task' "
        "AND i.item_id=? ORDER BY t.label_key", (business_id, task_id))]
    return {"row": dict(row), "assignees": assigned(uow, business_id, task_id), "blockers": blockers(uow, business_id, task_id),
            "tags": labels}


def changed_details(before: Mapping | None, after: Mapping) -> bool:
    """Whether anything but the state changed (the v1 'Task details changed' signal, relationships included)."""
    if before is None:
        return True
    quiet = {key for key in after["row"] if key in QUIET}
    return (any(before["row"].get(key) != value for key, value in after["row"].items() if key not in quiet)
            or any(before[key] != after[key] for key in ("assignees", "blockers", "tags")))


def describe(before: Mapping, after: Mapping) -> tuple[str, dict] | None:
    """The thread's system entry for a change: its text and structured change, or ``None`` when nothing changed."""
    moved = before["row"]["status"] != after["row"]["status"]
    details = changed_details(before, after)
    if not moved and not details:
        return None
    body = f"State changed from {before['row']['status']} to {after['row']['status']}." if moved else ""
    if details:
        body += (" " if body else "") + "Task details changed."
    return body, {"from": before["row"]["status"] if moved else None, "to": after["row"]["status"] if moved else None,
                  "details": details}


def record_change(uow, business_id, before: Mapping, after: Mapping, recorder_id) -> str | None:
    """Append the system entry for a change; returns the entry id, or ``None`` for a no-op."""
    described = describe(before, after)
    if described is None:
        return None
    body, change = described
    return system_entry(uow, ThreadItem(business_id, "task", after["row"]["id"]), recorder_id=recorder_id, body=body,
                        change=change)


def event_kinds(before: Mapping | None, after: Mapping, *, commented: bool) -> list[str]:
    """The v1 event types of an operation: created, updated (state or details), assignment_changed, note_added."""
    if before is None:
        kinds = ["created"]
    else:
        kinds = ["updated"] if before["row"]["status"] != after["row"]["status"] or changed_details(before, after) else []
        if before["assignees"] != after["assignees"]:
            kinds.append("assignment_changed")
    if commented:
        kinds.append("note_added")
    return kinds


def assignment_targets(uow, before: Mapping | None, after: Mapping) -> set[str]:
    """Agents whose assignment changed (added or removed): they are told whether or not anything else changed."""
    old = set(before["assignees"]) if before else set()
    moved = old ^ set(after["assignees"])
    if not moved:
        return set()
    agents = {row[0] for row in uow.connection.execute(
        "SELECT id FROM principals WHERE kind='agent' AND id IN (" + ",".join("?" for _ in moved) + ")", sorted(moved))}
    return agents


__all__ = ["announce", "assigned", "assignment_targets", "blockers", "changed_details", "dependency_followers", "describe",
           "event_kinds", "record_change", "routes", "state"]
