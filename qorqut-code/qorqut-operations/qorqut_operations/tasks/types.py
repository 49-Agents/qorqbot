"""Task presentation types, independent of the owner-request assignment contract.

The existing JSON payload stores these typed attributes under an engine-owned key
that cannot be a custom field name. Public reads/writes expose normal Task fields;
custom-field replacement never overwrites them. All changes use Task revisions,
prepared plans, history and notifications. No separate Story/Customer/Email record exists.

A Customer keeps a person/company kind, one free-text state label and a small contact
profile in the same payload. They are valid only for the Customer type and never move
the Task: the state is a label, not a lifecycle state.

Every type also holds two short lines that are the Task's own record of where it stands: ``status_line`` is what the owner
is told (shown wherever the Task is shown, so nobody keeps a second copy of it), and ``next_step`` is the one pending
action. Both are single lines stored only while they hold something.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from copy import deepcopy
import unicodedata

from qorqut_operations import owner_text
from qorqut_operations.contracts import DomainError

TYPES = ("task", "story", "customer", "email")
KEY = "$task"
CUSTOMER_KINDS = ("person", "company")
STATE_LIMIT = 120
NOTES_LIMIT = 20000
PROFILE_LIMITS = {"company": 500, "contact_name": 500, "email": 500, "phone": 500, "website": 500, "notes": NOTES_LIMIT}
LINE_LIMIT = 240
DEFAULTS = {"task_type": "task", "overview": "", "email_conversation_id": None,
            "customer_kind": None, "customer_state": "", "customer_profile": {},
            "status_line": "", "next_step": ""}
# Stored whenever the Task is not all-default, exactly as before Customers existed, so a repeated write of an older
# Story or Email Task stays a no-op. The customer attributes are stored only while they hold something.
STORED_ALWAYS = ("task_type", "overview", "email_conversation_id")
CUSTOMER = ("customer_kind", "customer_state", "customer_profile")
LINES = ("status_line", "next_step")
# The owner reads the status line, so it names things in words. A record identifier in it is a sign that working detail
# was pasted where a sentence belongs.
IDENTIFIER = owner_text.RECORD_ID

KIND_SCHEMA = {"anyOf": [{"enum": list(CUSTOMER_KINDS)}, {"type": "null"}],
               "description": instruction_text('types-person-or-company-for-a-customer-a-new')}
# The raw bound only limits payload size; the 120-character limit applies after whitespace normalization.
STATE_SCHEMA = {"type": "string", "maxLength": 1000,
                "description": instruction_text('types-free-text-customer-label-of-at-most-120')}
PROFILE_SCHEMA = {"type": "object", "additionalProperties": False,
                  "properties": {name: {"type": "string", "maxLength": limit} for name, limit in PROFILE_LIMITS.items()},
                  "description": instruction_text('types-customer-contact-profile-replaced-as-a-whole-the')}
# These descriptions are literal: a key in the tool-instruction catalog would have to be adopted by every installed
# catalog before the engine could start.
STATUS_LINE_SCHEMA = {"type": "string", "maxLength": 1000,
                      "description": f"Where this Task stands now, in one plain sentence of at most {LINE_LIMIT} characters written "
                                     "for the owner. Replaced as a whole; it is the text shown for the Task, so do not repeat it "
                                     "elsewhere. No record identifiers. Empty clears it."}
NEXT_STEP_SCHEMA = {"type": "string", "maxLength": 1000,
                    "description": f"The one pending action on this Task and who takes it, at most {LINE_LIMIT} characters. "
                                   "Replaced as a whole; empty when nothing is pending."}


def normalize_state(value):
    """The label with surrounding whitespace trimmed and every internal whitespace run collapsed to one space."""
    return " ".join(value.split())


def state_key(value):
    """Comparison key of a label: whitespace-collapsed, Unicode-normalized and case-folded."""
    folded = unicodedata.normalize("NFKC", normalize_state(value)).casefold()
    return normalize_state(unicodedata.normalize("NFKC", folded))


def resolve_state(value, known):
    """The display spelling for a requested label: the first-introduced spelling of an equal one, else its own."""
    shown = normalize_state(value)
    key = state_key(shown)
    return next((item for item in known if state_key(item) == key), shown)


def attributes(task):
    stored = task.get("fields_json", {}).get(KEY, {})
    return {**deepcopy(DEFAULTS), **deepcopy(stored)} if isinstance(stored, dict) else deepcopy(DEFAULTS)


def custom_fields(task):
    return {key: value for key, value in task["fields_json"].items() if key not in {KEY, "$task_links"}}


def apply_attributes(current, edits, states=()):
    """Apply the typed edits (popping them) and the custom-field replacement; ``states`` is the scope's label vocabulary."""
    links = deepcopy(current.get("fields_json", {}).get("$task_links", []))
    values = attributes(current)
    given = {key: edits.pop(key) for key in DEFAULTS if key in edits}
    values.update(given)
    if values["task_type"] != "email" and "email_conversation_id" not in given:
        # A historical Email Task keeps its binding as read-only data; changing the type away from email ends it, so the caller
        # never has to name (or look up) the conversation to do so.
        values["email_conversation_id"] = None
    if isinstance(given.get("customer_state"), str):
        values["customer_state"] = resolve_state(given["customer_state"], states)
    for name in LINES:
        if isinstance(given.get(name), str):
            values[name] = normalize_state(given[name])
    if values["status_line"] != attributes(current)["status_line"]:
        # Checked when the line changes, so a Task whose older line predates the rule can still be written.
        problem = owner_text.problem(values["status_line"])
        if problem:
            raise DomainError("validation_error", f"The status line is read by the owner, and {problem}: say where the work "
                                                  "stands in words and put the detail in a working note")
    if values["task_type"] == "customer" and values["customer_kind"] is None and "customer_kind" not in given:
        values["customer_kind"] = "person"
    if "fields" in edits:
        fields = edits.pop("fields")
        if KEY in fields or "$task_links" in fields:
            raise DomainError("validation_error", "Use the Task type and overview fields directly")
    else:
        fields = custom_fields(current)
    current["fields_json"] = deepcopy(fields)
    if links:
        current["fields_json"]["$task_links"] = links
    if values != DEFAULTS:
        current["fields_json"][KEY] = {name: value for name, value in values.items()
                                       if name in STORED_ALWAYS or value != DEFAULTS[name]}


def validate_customer(values):
    """The Customer attributes: kind, normalized state label and profile. Other types hold none of them."""
    kind, state, profile = (values[name] for name in CUSTOMER)
    if values["task_type"] != "customer":
        if kind is not None or state != "" or profile != {}:
            raise DomainError("validation_error", "Customer kind, state and profile belong to a Customer; clear them explicitly "
                                                  "(customer_kind null, customer_state empty, customer_profile {}) when changing the type")
        return
    if kind not in CUSTOMER_KINDS:
        raise DomainError("validation_error", "A Customer is a person or a company")
    if not isinstance(state, str) or state != normalize_state(state) or len(state) > STATE_LIMIT:
        raise DomainError("validation_error", f"Customer state must be a label of at most {STATE_LIMIT} characters with single spaces")
    if not isinstance(profile, dict) or set(profile) - set(PROFILE_LIMITS):
        raise DomainError("validation_error", "Customer profile may hold only " + ", ".join(PROFILE_LIMITS))
    for name, value in profile.items():
        if not isinstance(value, str) or len(value) > PROFILE_LIMITS[name]:
            raise DomainError("validation_error", f"Customer profile {name} must be text up to {PROFILE_LIMITS[name]} characters")


def validate_lines(values):
    """The status line and next step: each one line within the limit, and a status line free of record identifiers."""
    for name in LINES:
        value = values[name]
        if not isinstance(value, str) or value != normalize_state(value) or len(value) > LINE_LIMIT:
            raise DomainError("validation_error", f"Task {name} must be one line of at most {LINE_LIMIT} characters")
    if IDENTIFIER.search(values["status_line"]):
        raise DomainError("validation_error", "The status line is read by the owner: name things in words, not by record identifier")


def validate_attributes(task):
    links = task["fields_json"].get("$task_links", [])
    if (not isinstance(links, list) or len(links) > 100
            or any(not isinstance(item, dict) or set(item) != {"business_id", "task_id"}
                   or not isinstance(item["task_id"], str) or not item["task_id"]
                   or (item["business_id"] is not None and not isinstance(item["business_id"], str)) for item in links)):
        raise DomainError("validation_error", "Invalid Task links")
    raw = task["fields_json"].get(KEY, {})
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise DomainError("validation_error", "Invalid Task type attributes")
    values = attributes(task)
    if values["task_type"] not in TYPES:
        raise DomainError("validation_error", "Task type must be task, story, customer or email")
    if not isinstance(values["overview"], str) or len(values["overview"]) > 20000:
        raise DomainError("validation_error", "Task overview must be text up to 20000 characters")
    email = values["email_conversation_id"]
    if values["task_type"] == "email":
        if not isinstance(email, str) or not email:
            raise DomainError("validation_error", "Select an email conversation for an Email Task")
    elif email is not None:
        raise DomainError("validation_error", "Only an Email Task can bind an email conversation")
    validate_customer(values)
    validate_lines(values)
