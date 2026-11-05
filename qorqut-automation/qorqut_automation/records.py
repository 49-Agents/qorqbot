"""Names, bounds and pure helpers of the automation record (no I/O)."""

from __future__ import annotations

import json
import re

from qorqut_operations.contracts import DomainError

PROGRAM = "automation.run"
CAPABILITY = "manage_automations"
READ_CAPABILITY = "read_global_context"
SOURCE = "automations"
ITEM_TYPE = "automation"
TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,56}$")
STEP_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
# The owned authoring area: one Python tool entry per automation in this extension's own plugin package.
SCRIPT_PACKAGE = "scripting-and-automation"
SCRIPT_REF = re.compile(r"^" + SCRIPT_PACKAGE + r"/[a-z0-9][a-z0-9_-]{0,63}$")
GENERATED_ENTRY = re.compile(r"^" + SCRIPT_PACKAGE + r"/auto-([0-9a-f]{32})$")
PHASE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
OPERATION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
MAX_GRANTS = 50
MAX_ARGUMENT_BYTES = 32768
MAX_SCRIPT_BYTES = 262144
MAX_PUBLISHED_BYTES = 16384
SCOPE = {"scope": {"enum": ["business", "global"]}}


def namespace(ctx, args) -> str:
    """The operation namespace: ``scope: global`` or the selected Project (the routine and Signoff-input convention)."""
    return "global" if args.get("scope") == "global" else "business:" + (ctx.business_id or "")


def scope_business(ctx, args):
    """The ``business_id`` of an automation addressed by this call (``None`` for Global)."""
    return None if args.get("scope") == "global" else ctx.business_id


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:56].strip("-")
    return slug or "automation"


def script_ref_for(automation_id: str) -> str:
    """The script entry generated for one automation record. The name derives from the record's own immutable id - never its slug,
    which is only unique per Global/Project scope - so two records can never share or collide on one plugin-global entry."""
    return f"{SCRIPT_PACKAGE}/auto-{automation_id.replace('-', '')}"


def generated_owner(script_ref: str):
    """The 32-hex record id a generated ``auto-`` entry name embeds, or ``None`` for any other (hand-authored) entry."""
    match = GENERATED_ENTRY.fullmatch(script_ref)
    return match.group(1) if match else None


def entry_id(script_ref: str) -> str:
    return script_ref.split("/", 1)[1]


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def bounded(value, limit: int, what: str):
    if len(encoded(value)) > limit:
        raise DomainError("validation_error", f"{what} exceeds {limit} bytes")
    return value


MANAGER_FIELDS = ("id", "business_id", "slug", "title", "description", "state", "pause_reason", "script_ref", "arguments_json",
                  "phases_json", "routine_id", "grants_json", "restrictions_json", "saved_json", "created_by_principal_id",
                  "revision", "created_at", "updated_at")
READER_FIELDS = ("id", "business_id", "slug", "title", "description", "state", "pause_reason", "phases_json", "revision",
                 "created_at", "updated_at")


def public(row: dict) -> dict:
    """The authored record for its manager (the configured Ordubashy) or the owner, without the frozen admission."""
    return {key: row[key] for key in MANAGER_FIELDS}


def reader_view(row: dict) -> dict:
    """The safe projection for any other authorized reader: identity, title, description, state and phase names only. Never the
    script reference, input, grants, restrictions, saved cursor or who created it."""
    return {key: row[key] for key in READER_FIELDS}


GRANT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["operation"],
    "properties": {
        "operation": {"type": "string", "pattern": OPERATION_NAME.pattern},
        "capability": {"type": "string", "pattern": OPERATION_NAME.pattern,
                       "description": "The one Global capability the operation declares, when the grant also supplies it."},
        "resource_type": {"type": "string", "maxLength": 100,
                          "description": "Limits the grant to one record. Supported only for calls that name a single such record by one "
                                         "required <type>_id argument; a read is resolved to that exact record, and a mutation's whole "
                                         "prepared target set must be exactly that record. Other shapes are refused at approval."},
        "resource_id": {"type": "string", "maxLength": 200},
        "note": {"type": "string", "maxLength": 500}}}
PHASES_SCHEMA = {
    "type": "array", "maxItems": 30, "items": {
        "type": "object", "additionalProperties": False, "required": ["name"],
        "properties": {"name": {"type": "string", "pattern": PHASE_NAME.pattern},
                       "description": {"type": "string", "maxLength": 500}}}}
RESTRICTIONS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "allowed_operations": {"type": "array", "maxItems": 200, "uniqueItems": True,
                               "items": {"type": "string", "pattern": OPERATION_NAME.pattern},
                               "description": "When present - even empty - only these operations may be called."},
        "denied_operations": {"type": "array", "maxItems": 200, "uniqueItems": True,
                              "items": {"type": "string", "pattern": OPERATION_NAME.pattern}},
        "max_calls": {"type": "integer", "minimum": 1, "maximum": 20000},
        "max_run_seconds": {"type": "integer", "minimum": 60, "maximum": 14400}}}


def normalize_phases(phases) -> list:
    names = [item["name"] for item in phases]
    if len(set(names)) != len(names):
        raise DomainError("validation_error", "Phase names must be unique")
    return [dict(item) for item in phases]


def normalize_grants(grants) -> list:
    if len(grants) > MAX_GRANTS:
        raise DomainError("validation_error", f"At most {MAX_GRANTS} grants are kept")
    result = []
    for grant in grants:
        if ("resource_id" in grant) != ("resource_type" in grant):
            raise DomainError("validation_error", "A grant names both resource_type and resource_id, or neither")
        result.append(dict(grant))
    return result
