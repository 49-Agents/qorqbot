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
TYPED_NAMED = frozenset({"task_type", "overview", "customer_state"})


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


def record_change(uow, business_id, before: Mapping, after: Mapping, recorder_id) -> str | None:
    """Append the system entry for a change; returns the entry id, or ``None`` for a no-op."""
    described = describe(before, after, principal_names(uow))
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
           "detail_changes", "event_kinds", "principal_names", "record_change", "routes", "state", "workers"]
