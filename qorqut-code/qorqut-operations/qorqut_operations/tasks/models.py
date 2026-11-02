"""Task vocabulary and validation: fixed states, typed custom fields, dependencies, parents and the single snooze.

State handling is deliberately thin. The six states are a fixed enum; they gate nothing: unfinished children or
dependencies never block a state change, and a parent's state never moves its children. Only the owning scope's custom
field definitions are configurable, and the label vocabulary is the shared catalog, not a per-scope list."""

from __future__ import annotations

from datetime import date, datetime, timezone
from graphlib import CycleError, TopologicalSorter
import re

from qorqut_operations.application.plans import timestamp
from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.schema import CLOSED_STATES, KINDS, PAUSED_STATES, STATES
from qorqut_operations.tasks.types import custom_fields, validate_attributes

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FIELD_TYPES = ("string", "int", "bool", "date", "enum")
RESERVED_FIELD_NAMES = frozenset({
    "id", "title", "status", "assignee_id", "assignee_ids", "archived", "owner", "tags", "due", "blocked_by", "fields", "notes",
    "worker_ids", "workers", "user_needed", "latest_completed_at", "revision", "kind", "parent_id", "snoozed_until", "snooze", "note", "note_refs", "summary", "relay", "focus"})
DEFAULT_STATE = "open"
STATE_DEFINITIONS = tuple(
    {"name": name, "kind": "closed" if name in CLOSED_STATES else "open",
     **({"default": True} if name == DEFAULT_STATE else {}), **({"paused": True} if name in PAUSED_STATES else {})}
    for name in STATES)
TAG_REFERENCE = {"source": "catalog", "operation": "tags_list"}
DEFAULT_CONFIG = {"v": 1, "fields": {}}
CONFIG_KEYS = frozenset({"v", "states", "tags", "fields"})


def invalid(message):
    raise DomainError("validation_error", message)


def full_config(fields) -> dict:
    """The configuration as read: fixed states, the catalog reference and the scope's own field definitions."""
    return {"v": 1, "states": [dict(state) for state in STATE_DEFINITIONS], "tags": dict(TAG_REFERENCE), "fields": dict(fields)}


def stored_fields(config) -> dict:
    """Field definitions of a stored configuration row. Retired state/tag keys of an older row are ignored."""
    value = (config or {}).get("fields", {})
    return value if isinstance(value, dict) else {}


def validate_config(value) -> dict:
    """The accepted configuration update: ``{"v": 1, "fields": {...}}``. Returns the field definitions.

    ``states`` and ``tags`` may be echoed back only exactly as ``tasks_config_get`` shows them (so a read-modify-write
    client works); any other value is a refused attempt to configure what is now fixed or shared."""
    if not isinstance(value, dict) or value.get("v") != 1 or set(value) - CONFIG_KEYS:
        invalid("Task configuration is {v:1, fields}; states are fixed and tags come from the shared catalog")
    if "states" in value and value["states"] != list(STATE_DEFINITIONS):
        invalid("Task states are fixed (open, working, blocked, waiting, done, cancelled) and cannot be reconfigured")
    if "tags" in value and value["tags"] != TAG_REFERENCE:
        invalid("Task tags come from the shared tag catalog (tags_list/tags_create) and cannot be configured per scope")
    fields = value.get("fields", {})
    if not isinstance(fields, dict):
        invalid("Task fields must be an object")
    for name, spec in fields.items():
        if not NAME.fullmatch(name) or name in RESERVED_FIELD_NAMES or not isinstance(spec, dict) or spec.get("type") not in FIELD_TYPES:
            invalid("Invalid custom task field")
        if spec["type"] == "enum" and (not isinstance(spec.get("values"), list) or not spec["values"]):
            invalid("Enum task fields need permitted values")
    return fields


def is_date(value) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def validate_task(task, fields):
    """The retained v1 task rules against the current field definitions (``fields``)."""
    title = task["title"]
    if not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title or len(title) > 1000:
        invalid("Task title must be a non-empty single line up to 1000 characters")
    if task["status"] not in STATES:
        invalid("Task status must be one of " + ", ".join(STATES))
    if task.get("kind", "ordinary") not in KINDS:
        invalid("Task kind must be ordinary or owner_request")
    if task.get("due") is not None and not is_date(task["due"]):
        invalid("Task due date must be a real YYYY-MM-DD date")
    values = task["fields_json"]
    if not isinstance(values, dict):
        invalid("Task fields must be an object")
    validate_attributes(task)
    for name, value in custom_fields(task).items():
        spec = fields.get(name)
        if spec is None:
            invalid("Unknown custom task field: " + name)
        kind = spec["type"]
        if ((kind == "string" and not isinstance(value, str)) or (kind == "int" and type(value) is not int)
                or (kind == "bool" and type(value) is not bool) or (kind == "enum" and value not in spec["values"])):
            invalid("Custom task field has wrong type: " + name)
        if kind == "date" and not is_date(value):
            invalid("Custom date field is invalid: " + name)


def validate_dependencies(task_id, blockers, graph):
    """Informational ``blocked_by`` links: distinct, existing in the same scope, never the task itself, never a cycle."""
    if task_id in blockers or len(set(blockers)) != len(blockers):
        invalid("Task blockers must be unique and cannot include the task itself")
    if set(blockers) - set(graph):
        invalid("Every blocker must exist in the same owning scope")
    try:
        tuple(TopologicalSorter({**graph, task_id: set(blockers)}).static_order())
    except CycleError:
        invalid("Task dependencies cannot contain cycles")


def validate_parent(task_id, parent_id, parents):
    """``parents`` maps every Task of the scope to its parent id; the new link must exist and cannot close a loop."""
    if parent_id is None:
        return
    if parent_id == task_id:
        invalid("A Task cannot be its own parent")
    if parent_id not in parents:
        invalid("The parent Task must exist in the same owning scope")
    seen, current = {task_id}, parent_id
    while current is not None:
        if current in seen:
            invalid("Task parents cannot contain a cycle")
        seen.add(current)
        current = parents.get(current)


def normalize_snooze(value, now, current=None):
    """The stored ``snoozed_until`` for a requested one, or ``None`` to clear.

    A value equal to the stored one is accepted unchanged even when it has since passed, so repeating a write is
    idempotent; any other value must be a timezone-aware time after ``now``."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        invalid("snoozed_until must be a timezone-aware timestamp or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        normalized = timestamp(lambda: parsed.astimezone(timezone.utc))
    except (ValueError, OverflowError):
        invalid("snoozed_until must be a future timestamp with a timezone")
    if normalized != current and normalized <= now:
        invalid("snoozed_until must be a future timestamp with a timezone")
    return normalized


def snooze_view(task, now) -> dict:
    until = task.get("snoozed_until")
    return {"snoozed_until": until, "snoozed": bool(until and until > now), "snooze_generation": task.get("snooze_generation", 0)}
