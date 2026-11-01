"""Registry-facing routine operations, as plain OperationSpec field mappings.

Operations deliberately does not import the application package; the registry
side builds its OperationSpec from these mappings (see api.register_definitions).
"""

from __future__ import annotations

from . import service

_TEXT = {"type": "string", "minLength": 1}
_KEY = {**_TEXT, "description": "Idempotency key: retrying the same request with it is safe."}
_IDS = {"type": "array", "items": _TEXT}
_TASK_DEFINITION = {
    "type": "object",
    "description": "Fields of the fresh Task each occurrence creates.",
    "properties": {
        "title": _TEXT, "description": {"type": "string"}, "assignee_ids": _IDS,
        "parent_id": {"type": ["string", "null"]}, "dependency_ids": _IDS,
    },
    "required": ["title"], "additionalProperties": False,
}
_SCRIPT_DEFINITION = {
    "type": "object",
    "description": "A program run directly (no shell) from a directory inside Operations.",
    "properties": {
        "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "cwd": {**_TEXT, "description": "Relative directory inside Operations."},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
    },
    "required": ["argv", "cwd", "timeout_seconds"], "additionalProperties": False,
}
_DEFINITION = {"oneOf": [_TASK_DEFINITION, _SCRIPT_DEFINITION]}
_SCHEDULE = {**_TEXT, "description": "Five-field cron expression."}
_TIMEZONE = {**_TEXT, "description": "Explicit IANA time zone name, for example Europe/London."}
_STATUS = {"type": "string", "enum": list(service.ROUTINE_STATES)}
_KIND = {"type": "string", "enum": list(service.ROUTINE_KINDS)}
_BOTH = frozenset({"owner", "agent"})


def _spec(name, description, properties, required, handler, unit):
    return {
        "name": name, "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required,
                         "additionalProperties": False},
        "handler": handler, "unit": unit, "exposure": _BOTH, "supports_signoff": False,
    }


def operation_definitions() -> list[dict]:
    """Routine operations for the shared registry; none of them accepts a Signoff grant."""
    return [
        _spec("routines_create",
              "Create a scheduled routine: a Task routine makes a fresh Task each occurrence, a script "
              "routine runs a local program. Tags set its scope and are inherited by its Tasks.",
              {"title": _TEXT, "kind": _KIND, "schedule": _SCHEDULE, "timezone": _TIMEZONE,
               "definition": _DEFINITION, "idempotency_key": _KEY, "description": {"type": "string"},
               "status": _STATUS, "tag_ids": _IDS},
              ["title", "kind", "schedule", "timezone", "definition", "idempotency_key"],
              service.create_routine, "write"),
        _spec("routines_update",
              "Edit a routine at its expected revision. Kind cannot change. A schedule change or resume "
              "chooses the next future slot; saved runs are not rewritten.",
              {"routine_id": _TEXT, "expected_revision": {"type": "integer", "minimum": 1},
               "idempotency_key": _KEY, "title": _TEXT, "description": {"type": "string"},
               "schedule": _SCHEDULE, "timezone": _TIMEZONE, "definition": _DEFINITION, "status": _STATUS,
               "tag_ids": _IDS},
              ["routine_id", "expected_revision", "idempotency_key"], service.update_routine, "write"),
        _spec("routines_get", "Read one routine, including its next due time and any blocking script run.",
              {"routine_id": _TEXT}, ["routine_id"], service.get_routine, "read"),
        _spec("routines_list", "List routines you can read, optionally by tag, status or kind.",
              {"tag_id": _TEXT, "status": _STATUS, "kind": _KIND}, [], service.list_routines, "read"),
        _spec("routines_preview", "Preview the next occurrences of a cron schedule in an IANA time zone.",
              {"schedule": _SCHEDULE, "timezone": _TIMEZONE, "after": _TEXT,
               "count": {"type": "integer", "minimum": 1, "maximum": service.MAX_PREVIEW}},
              ["schedule", "timezone"], service.preview_schedule, "read"),
        _spec("routines_run",
              "Run a routine now, even while paused, without changing its schedule. retry_of links a fresh "
              "run to a failed or uncertain one. A script routine with an unfinished or unresolved run "
              "returns routine_busy.",
              {"routine_id": _TEXT, "expected_revision": {"type": "integer", "minimum": 1},
               "idempotency_key": _KEY, "retry_of": _TEXT},
              ["routine_id", "expected_revision", "idempotency_key"], service.request_run, "write"),
        _spec("routines_runs_list", "List routine runs, newest first, with their outcomes.",
              {"routine_id": _TEXT, "offset": {"type": "integer", "minimum": 0},
               "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, [], service.list_runs, "read"),
        _spec("routines_resolve_run",
              "Record explicit confirmation that an uncertain script run's process is stopped, ending its "
              "overlap block. The run stays uncertain, and nothing is started or retried.",
              {"run_id": _TEXT, "expected_state": {"type": "string", "enum": ["uncertain"]},
               "confirmation": _TEXT, "idempotency_key": _KEY},
              ["run_id", "expected_state", "confirmation", "idempotency_key"], service.resolve_run, "write"),
    ]
