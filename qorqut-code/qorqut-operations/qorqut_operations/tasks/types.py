"""Task presentation types, independent of the owner-request assignment contract.

The existing JSON payload stores these typed attributes under an engine-owned key
that cannot be a custom field name. Public reads/writes expose normal Task fields;
custom-field replacement never overwrites them. All changes use Task revisions,
prepared plans, history and notifications. No separate Story/Email record exists.
"""
from copy import deepcopy

from qorqut_operations.contracts import DomainError

TYPES = ("task", "story", "email")
KEY = "$task"
DEFAULTS = {"task_type": "task", "overview": "", "email_conversation_id": None}


def attributes(task):
    return {**DEFAULTS, **task.get("fields_json", {}).get(KEY, {})}


def custom_fields(task):
    return {key: value for key, value in task["fields_json"].items() if key != KEY}


def apply_attributes(current, edits):
    values = attributes(current)
    for key in DEFAULTS:
        if key in edits:
            values[key] = edits.pop(key)
    if "fields" in edits:
        fields = edits.pop("fields")
        if KEY in fields:
            raise DomainError("validation_error", "Use the Task type and overview fields directly")
    else:
        fields = custom_fields(current)
    current["fields_json"] = deepcopy(fields)
    if values != DEFAULTS:
        current["fields_json"][KEY] = values


def validate_attributes(task):
    raw = task["fields_json"].get(KEY, {})
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise DomainError("validation_error", "Invalid Task type attributes")
    values = attributes(task)
    if values["task_type"] not in TYPES:
        raise DomainError("validation_error", "Task type must be task, story or email")
    if not isinstance(values["overview"], str) or len(values["overview"]) > 20000:
        raise DomainError("validation_error", "Task overview must be text up to 20000 characters")
    email = values["email_conversation_id"]
    if values["task_type"] == "email":
        if not isinstance(email, str) or not email:
            raise DomainError("validation_error", "Select an email conversation for an Email Task")
    elif email is not None:
        raise DomainError("validation_error", "Only an Email Task can bind an email conversation")
