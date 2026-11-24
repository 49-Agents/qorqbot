"""The one writer of Task inbox events and the thread entries that describe a change.

A Task event addresses two independent sets. *Required* recipients are a responsibility or a mandatory route: the final
assignees of a created, changed or reassigned Task, every assignment target (a removed assignee learns of it too), the
assignees of Tasks that depend on this one, and the routes the host makes mandatory (coordinator routing of owner work,
the Focus coordinator). *Optional* recipients are the Task's followers and the host's discretionary watchers. A follow
never removes a required delivery, and unfollowing never stops required ones; the acting principal is never woken by
its own change. All of it runs inside the writing transaction."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from qorqut_operations.discussion.events import emit_event
from qorqut_operations.discussion.follows import ThreadItem, followers
from qorqut_operations.discussion.threads import attach_event, system_entry
from qorqut_operations.tasks.types import DEFAULTS as TYPED_DEFAULTS, KEY as TYPED_KEY

SOURCE_TYPE = "tasks"
# Row columns whose change is not, by itself, a change of the Task's details. ``latest_completed_at`` only restates a
# move to done, which the state sentence already reports.
QUIET = frozenset({"revision", "updated_at", "status", "snooze_generation", "latest_completed_at"})
# How many names one sentence of a system entry lists before it counts the rest.
NAMED = 5


def assigned(uow, project_id, task_id, *, agents_only: bool = False) -> list[str]:
    """The Task's assignees (``assigned_to`` edges), agents and people or agents only."""
    return [row[0] for row in uow.connection.execute(
        "SELECT to_id FROM relationships WHERE project_id IS ? AND from_type='task' AND from_id=? AND role='assigned_to'"
        + (" AND to_type='agent'" if agents_only else "") + " ORDER BY to_id", (project_id, task_id))]


def blockers(uow, project_id, task_id) -> list[str]:
    return [row[0] for row in uow.connection.execute(
        "SELECT DISTINCT to_id FROM relationships WHERE project_id IS ? AND from_type='task' AND from_id=? "
        "AND to_type='task' AND role='blocked_by' ORDER BY to_id", (project_id, task_id))]


def dependency_followers(uow, project_id, task_id) -> set[str]:
    """Agents responsible for Tasks that are blocked by this one (derived from the graph, never subscribed)."""
    return {row[0] for row in uow.connection.execute(
        "SELECT DISTINCT a.to_id FROM relationships d JOIN relationships a ON a.project_id IS d.project_id AND a.from_type='task' "
        "AND a.from_id=d.from_id AND a.role='assigned_to' AND a.to_type='agent' WHERE d.project_id IS ? "
        "AND d.from_type='task' AND d.to_type='task' AND d.role='blocked_by' AND d.to_id=?", (project_id, task_id))}


def change(before: Mapping | None, after: Mapping) -> dict:
    """What an operation did to the Task's state and workers, for the host's routing: the stored state before (``None``
    for a new Task) and after, and the workers recorded after it that were not recorded before."""
    workers = lambda state: set(json.loads(state["row"].get("worker_ids_json") or "[]")) if state else set()
    return {"from": before["row"]["status"] if before else None, "to": after["row"]["status"],
            "new_workers": sorted(workers(after) - workers(before))}


def routes(uow, routing, project_id, task: Mapping, actor_id, *, responsibility: bool, targets: Iterable[str] = (),
           change: Mapping | None = None):
    """``(required, optional)`` principal ids for an event about ``task``; ``change`` is passed on to the host's routing."""
    task_id = task["id"]
    required = set(targets) | dependency_followers(uow, project_id, task_id)
    if responsibility:
        required |= set(assigned(uow, project_id, task_id, agents_only=True))
    host_required, host_optional = routing.routes(uow, project_id, task, actor_id, change)
    required |= set(host_required)
    optional = set(followers(uow, ThreadItem(project_id, "task", task_id))) | set(host_optional)
    return required, optional


def announce(uow, routing, project_id, task: Mapping, actor_id, kinds, *, responsibility: bool, targets: Iterable[str] = (),
             summary=None, messages: Iterable[str] = (), change: Mapping | None = None) -> int | None:
    """Emit the one event for an operation on a Task and bind the thread entries written with it."""
    required, optional = routes(uow, routing, project_id, task, actor_id, responsibility=responsibility, targets=targets,
                                change=change)
    event_id = emit_event(uow, project_id=project_id, source_type=SOURCE_TYPE, source_id=task["id"],
                          source_revision=task["revision"], actor_id=actor_id, event_type=",".join(kinds),
                          summary=summary or task["title"], required=required, optional=optional)
    if event_id is not None:
        attach_event(uow, ThreadItem(project_id, "task", task["id"]), messages, event_id)
    return event_id


# What happens to one step of a Task's plan. These are told to exactly the principals the host names, never to the
# Task's followers: ``plan_step_started`` and ``plan_step_done`` (a step tagged for it became the step in hand, or was
# finished) and ``plan_step_submitted`` (a step waits for confirmation) go to the coordinator; ``plan_step_approved``,
# ``plan_step_returned`` and ``plan_step_skipped`` (the decision on a submitted step) go to the Task's workers.
STEP_EVENTS = ("plan_step_started", "plan_step_done", "plan_step_submitted", "plan_step_approved", "plan_step_returned",
               "plan_step_skipped")
SETTLED = "Settled: the step no longer waits for confirmation"


def announce_step(uow, project_id, task: Mapping, actor_id, kinds, summary: str, recipients: Iterable[str],
                  marker: Mapping | None = None) -> int | None:
    """Emit the one event for what an action on a Task's plan did to its steps, to ``recipients`` only.

    A step action does not write the Task, so the event carries the Task's unchanged revision and no thread entry.
    ``marker`` is kept on the event: ``plan_notice`` names the engine operation and the side that was told, so the host
    can tell that this was already said, and ``plan_operation`` and ``plan_step`` name the operation and its step."""
    unknown = [kind for kind in kinds if kind not in STEP_EVENTS]
    if unknown or not kinds:
        raise ValueError(f"Unknown plan step event: {unknown}")
    return emit_event(uow, project_id=project_id, source_type=SOURCE_TYPE, source_id=task["id"],
                      source_revision=task["revision"], actor_id=actor_id, event_type=",".join(kinds), summary=summary,
                      required=recipients, visibility=dict(marker) if marker else None)


def step_announced(uow, project_id, task_id, notice: str) -> bool:
    """Whether the plan step event marked ``plan_notice: notice`` was already written for this Task."""
    return uow.connection.execute(
        "SELECT 1 FROM inbox_events WHERE source_type=? AND source_id=? AND project_id IS ? "
        "AND json_extract(visibility_json,'$.plan_notice')=? LIMIT 1", (SOURCE_TYPE, task_id, project_id, notice)).fetchone() is not None


def settle_submissions(uow, project_id, task_id, operations: Iterable[str] | None = None, *, at) -> int:
    """Acknowledge the pending deliveries that say a step of this Task's plan waits for confirmation; returns how many.

    ``operations`` are the engine operations the steps were handed in with: only their events are settled, when the
    step was decided or skipped. Left out, every such event of the Task is settled: the Task was closed or its plan
    replaced, so nothing of it waits any more."""
    chosen = None if operations is None else list(operations)
    if chosen is not None and not chosen:
        return 0
    narrow = "" if chosen is None else (" AND json_extract(e.visibility_json,'$.plan_operation') IN ("
                                        + ",".join("?" for _ in chosen) + ")")
    cursor = uow.connection.execute(
        "UPDATE inbox_deliveries SET acknowledged_at=?,outcome=? WHERE acknowledged_at IS NULL AND event_id IN ("
        "SELECT e.id FROM inbox_events e WHERE e.source_type=? AND e.source_id=? AND e.project_id IS ? "
        "AND e.event_type LIKE '%plan_step_submitted%'" + narrow + ")",
        (at, SETTLED, SOURCE_TYPE, task_id, project_id, *(chosen or ())))
    return cursor.rowcount


def state(uow, project_id, task_id) -> dict | None:
    """Everything a change of the Task can alter, for comparing before and after one operation."""
    row = uow.connection.execute("SELECT * FROM tasks WHERE project_id IS ? AND id=?", (project_id, task_id)).fetchone()
    if row is None:
        return None
    labels = [label[0] for label in uow.connection.execute(
        "SELECT t.label_key FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.project_id IS ? AND i.item_type='task' "
        "AND i.item_id=? ORDER BY t.label_key", (project_id, task_id))]
    return {"row": dict(row), "assignees": assigned(uow, project_id, task_id), "blockers": blockers(uow, project_id, task_id),
            "tags": labels}


def changed_details(before: Mapping | None, after: Mapping) -> bool:
    """Whether anything but the state changed (the v1 'Task details changed' signal, relationships included)."""
    if before is None:
        return True
    quiet = {key for key in after["row"] if key in QUIET}
    return (any(before["row"].get(key) != value for key, value in after["row"].items() if key not in quiet)
            or any(before[key] != after[key] for key in ("assignees", "blockers", "tags")))


def workers(state: Mapping | None) -> list[str]:
    """The Task's Workers (who must act next) in a ``state`` snapshot; the raw row keeps them as JSON text."""
    value = state["row"].get("worker_ids_json") if state else None
    if isinstance(value, str):
        value = json.loads(value or "[]")
    return list(value or [])


def _stored(row: Mapping, key: str) -> dict:
    value = row.get(key)
    if isinstance(value, str):
        value = json.loads(value or "{}")
    return value if isinstance(value, dict) else {}


# Typed attributes that get a sentence of their own; every other stored field is reported as ``Fields changed.``.
# ``queue_rank`` only orders a worker's queue and is never reported.
TYPED_NAMED = frozenset({"task_type", "overview", "customer_state", "status_line", "next_step", "plan", "queue_rank",
                         "plan_template"})


def _typed(fields: Mapping) -> dict:
    """The typed attributes with their defaults, so a Task that stored none compares equal to one that stored defaults."""
    value = fields.get(TYPED_KEY)
    return {**TYPED_DEFAULTS, **(value if isinstance(value, dict) else {})}


def _unnamed(fields: Mapping, typed: Mapping) -> tuple:
    return ({key: value for key, value in fields.items() if key != TYPED_KEY},
            {key: value for key, value in typed.items() if key not in TYPED_NAMED})


def _listed(values) -> str:
    values = list(values)
    shown = ", ".join(str(value) for value in values[:NAMED])
    return shown + (f" and {len(values) - NAMED} more" if len(values) > NAMED else "")


def _moved(label: str, old, new, names=None) -> str | None:
    """``Label: added a, b; removed c.`` for two sets, or ``None`` when they are equal."""
    added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
    if not added and not removed:
        return None
    show = names or (lambda values: values)
    parts = (["added " + _listed(show(added))] if added else []) + (["removed " + _listed(show(removed))] if removed else [])
    return f"{label}: " + "; ".join(parts) + "."


def detail_changes(before: Mapping, after: Mapping, names=None) -> list[tuple[str, str]]:
    """What changed besides the state, as ``(field, sentence)`` pairs a reader can act on without opening the Task.

    Only short, structured values are named (a date, a principal, a tag, a type); authored text such as a title or
    an overview is reported as changed and never copied. ``names`` maps principal ids to display names."""
    old, new, found = before["row"], after["row"], []

    def add(field, sentence):
        if sentence:
            found.append((field, sentence))

    if old.get("title") != new.get("title"):
        add("title", "Title changed.")
    if old.get("due") != new.get("due"):
        add("due", f"Due set to {new['due']}." if new.get("due") else "Due cleared.")
    add("workers", _moved("Workers", workers(before), workers(after), names))
    add("assignees", _moved("Assignees", before["assignees"], after["assignees"], names))
    added, removed = set(after["blockers"]) - set(before["blockers"]), set(before["blockers"]) - set(after["blockers"])
    if added or removed:
        add("blockers", "Blockers: " + "; ".join(([f"{len(added)} added"] if added else [])
                                                 + ([f"{len(removed)} removed"] if removed else [])) + ".")
    add("tags", _moved("Tags", before["tags"], after["tags"]))
    if old.get("parent_id") != new.get("parent_id"):
        add("parent", "Parent changed." if old.get("parent_id") and new.get("parent_id")
            else "Parent set." if new.get("parent_id") else "Parent cleared.")
    if old.get("snoozed_until") != new.get("snoozed_until"):
        add("snooze", f"Snoozed until {new['snoozed_until']}." if new.get("snoozed_until") else "Snooze cleared.")
    if bool(old.get("archived")) != bool(new.get("archived")):
        add("archived", "Archived." if new.get("archived") else "Restored.")
    old_fields, new_fields = _stored(old, "fields_json"), _stored(new, "fields_json")
    old_typed, new_typed = _typed(old_fields), _typed(new_fields)
    if old_typed["task_type"] != new_typed["task_type"]:
        add("task_type", f"Type changed to {new_typed['task_type']}.")
    if (old_typed.get("overview") or "") != (new_typed.get("overview") or ""):
        add("overview", "Overview updated.")
    for field, label in (("status_line", "Status line"), ("next_step", "Next step")):
        if (old_typed.get(field) or "") != (new_typed.get(field) or ""):
            add(field, f"{label}: {new_typed[field]}" if new_typed.get(field) else f"{label} cleared.")
    if ((old_typed.get("plan") or []) != (new_typed.get("plan") or [])
            or old_typed.get("plan_template") != new_typed.get("plan_template")):
        count = len(new_typed.get("plan") or [])
        source = f" from the template {new_typed['plan_template']}" if new_typed.get("plan_template") else ""
        add("plan", f"Plan set: {count} step{'' if count == 1 else 's'}{source}." if count else "Plan removed.")
    if (old_typed.get("customer_state") or "") != (new_typed.get("customer_state") or ""):
        add("customer_state", f"Customer state set to {new_typed['customer_state']}." if new_typed.get("customer_state")
            else "Customer state cleared.")
    if _unnamed(old_fields, old_typed) != _unnamed(new_fields, new_typed):
        add("fields", "Fields changed.")
    return found


def describe(before: Mapping, after: Mapping, names=None) -> tuple[str, dict] | None:
    """The thread's system entry for a change: its text and structured change, or ``None`` when nothing changed.

    The text names what changed (``Workers: added …``, ``Due set to …``) so a reader of the thread or of the event
    need not open the Task to learn it; a change with no sentence of its own is still ``Task details changed.``.
    ``from``, ``to`` and ``details`` keep their meaning, and ``fields`` lists the changed details by name."""
    moved = before["row"]["status"] != after["row"]["status"]
    details = changed_details(before, after)
    if not moved and not details:
        return None
    found = detail_changes(before, after, names) if details else []
    sentences = ([f"State changed from {before['row']['status']} to {after['row']['status']}."] if moved else [])
    sentences += [sentence for _, sentence in found] or (["Task details changed."] if details else [])
    return " ".join(sentences), {"from": before["row"]["status"] if moved else None,
                                 "to": after["row"]["status"] if moved else None, "details": details,
                                 "fields": [field for field, _ in found]}


def principal_names(uow):
    """Display names for principal ids, falling back to the id itself for one that is no longer listed."""
    def names(ids):
        found = []
        for identity in ids:
            row = uow.connection.execute("SELECT display_name FROM principals WHERE id=?", (identity,)).fetchone()
            found.append(row[0] if row and row[0] else identity)
        return found
    return names


def record_change(uow, project_id, before: Mapping, after: Mapping, recorder_id) -> str | None:
    """Append the system entry for a change; returns the entry id, or ``None`` for a no-op."""
    described = describe(before, after, principal_names(uow))
    if described is None:
        return None
    body, change = described
    return system_entry(uow, ThreadItem(project_id, "task", after["row"]["id"]), recorder_id=recorder_id, body=body,
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


__all__ = ["SETTLED", "STEP_EVENTS", "announce", "announce_step", "assigned", "assignment_targets", "blockers", "change", "changed_details", "dependency_followers",
           "describe", "detail_changes", "event_kinds", "principal_names", "record_change", "routes", "settle_submissions", "state",
           "step_announced", "workers"]
