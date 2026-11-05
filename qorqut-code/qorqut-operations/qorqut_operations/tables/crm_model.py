"""The fixed kind, stage configuration and column bindings of a CRM table: pure validation, no storage.

A table is created either ``general`` (typed data rows) or ``crm`` (memberships that point at existing Task records) and
keeps that kind. A CRM's configuration holds bounded pipeline stages with stable IDs; a stage may be retired (kept for
the history of the memberships that name it) but a referenced stage is never removed. A CRM column is a local
membership field, or, with ``record_field``, a live projection of one whitelisted Task field. Nothing here evaluates
an expression: the whitelist is the whole vocabulary, so a column can never name an arbitrary path or a provider lookup.
"""

from __future__ import annotations

import re

from qorqut_operations.contracts import DomainError

GENERAL, CRM = "general", "crm"
KINDS = (GENERAL, CRM)
KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_STAGES, MAX_COLUMNS = 100, 100
MAX_NAME = 200

# The Task fields a CRM column may project, with the column type each one agrees with. Customer-only fields render null on
# other Task subtypes; ``fields.<id>`` is checked against the Task scope's custom field definitions.
RECORD_FIELDS = {
    "title": "text", "task_type": "text", "status": "text", "overview": "text", "customer_kind": "text",
    "customer_state": "text", "customer_profile.company": "text", "customer_profile.contact_name": "text",
    "customer_profile.email": "text", "customer_profile.phone": "text", "customer_profile.website": "text",
    "customer_profile.notes": "text", "tags": "json"}
CUSTOM_PREFIX = "fields."
# A Task custom field definition type and the CRM column type that can hold it.
CUSTOM_TYPES = {"string": "text", "int": "number", "bool": "boolean", "date": "date", "enum": "text"}

DEFAULT_COLUMNS = (
    {"id": "record_name", "name": "Name", "type": "text", "record_field": "title"},
    {"id": "roles", "name": "Roles", "type": "json", "record_field": "tags"},
    {"id": "email", "name": "Email", "type": "text", "record_field": "customer_profile.email"},
)


def invalid(message, **details):
    raise DomainError("validation_error", message, details=details or None)


def default_config():
    """A new CRM has no configured stages: the owner chooses them."""
    return {"stages": []}


def default_columns():
    return [dict(column) for column in DEFAULT_COLUMNS]


def kind_checked(kind):
    if kind not in KINDS:
        invalid("A table kind is general or crm")
    return kind


def config_checked(config):
    """The normalized CRM configuration ``{"stages": [{id, name, archived?}]}``; names are trimmed."""
    if not isinstance(config, dict) or set(config) - {"stages"} or not isinstance(config.get("stages", []), list):
        invalid("A CRM configuration is {stages: [{id, name, archived?}]}")
    stages, ids, names = [], set(), set()
    if len(config.get("stages", [])) > MAX_STAGES:
        invalid(f"A CRM supports up to {MAX_STAGES} stages")
    for stage in config.get("stages", []):
        if not isinstance(stage, dict) or set(stage) - {"id", "name", "archived"}:
            invalid("A stage is {id, name, archived?}")
        identity, name = stage.get("id"), stage.get("name")
        if not isinstance(identity, str) or not KEY.fullmatch(identity):
            invalid("A stage ID is a stable lowercase key")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME:
            invalid(f"A stage name is 1-{MAX_NAME} characters")
        if "archived" in stage and type(stage["archived"]) is not bool:
            invalid("A stage's archived flag is true or false")
        folded = " ".join(name.split()).casefold()
        if identity in ids or folded in names:
            invalid("Stage IDs and names must be unique", stage_id=identity)
        ids.add(identity)
        names.add(folded)
        stages.append({"id": identity, "name": name.strip(), **({"archived": True} if stage.get("archived") else {})})
    return {"stages": stages}


def stage_ids(config):
    return {stage["id"] for stage in (config or {}).get("stages", [])}


def stage_checked(config, stage_id, *, current=None):
    """A stage a membership may hold: configured, and not retired unless it already holds it. ``None`` is unset."""
    if stage_id is None:
        return None
    stage = next((s for s in (config or {}).get("stages", []) if s["id"] == stage_id), None)
    if stage is None:
        invalid("The stage is not configured on this CRM", stage_id=stage_id)
    if stage.get("archived") and stage_id != current:
        invalid("The stage is retired; choose an active stage or leave it unset", stage_id=stage_id)
    return stage_id


def stage_removal_checked(before, after, referenced):
    """Refuse removing a stage that a membership (removed ones included) still names."""
    gone = (stage_ids(before) - stage_ids(after)) & set(referenced)
    if gone:
        invalid("A referenced stage cannot be removed; move the affected entries first or retire the stage",
                stage_ids=sorted(gone))


def record_field_type(field, definitions=None):
    """The column type a ``record_field`` projects, or a refusal. ``definitions`` are the table scope's Task custom fields.

    A custom field absent from the table's scope is accepted (a linked Task may live in another scope and renders null
    where it has no such field); one that exists must agree with the column type."""
    if field in RECORD_FIELDS:
        return RECORD_FIELDS[field]
    if isinstance(field, str) and field.startswith(CUSTOM_PREFIX):
        name = field[len(CUSTOM_PREFIX):]
        if not re.fullmatch(r"^[a-z0-9][a-z0-9_-]{0,31}$", name):
            invalid("A projected custom field names an existing Task field", record_field=field)
        spec = (definitions or {}).get(name)
        return CUSTOM_TYPES[spec["type"]] if spec else None
    invalid("record_field is not a projectable Task field", record_field=field)


def columns_validated(columns, definitions=None, *, crm):
    """The normalized column list. Only a CRM may bind a column to a Task field."""
    if not isinstance(columns, list) or not columns or len(columns) > MAX_COLUMNS:
        invalid(f"A table has 1-{MAX_COLUMNS} columns")
    ids, names, result = set(), set(), []
    for column in columns:
        if not isinstance(column, dict):
            invalid("A column is {id, name, type}")
        identity, name = column["id"], column["name"].strip().casefold()
        if not name or identity in ids or name in names:
            invalid("Column IDs and names must be non-empty and unique")
        ids.add(identity)
        names.add(name)
        field = column.get("record_field")
        if field is not None:
            if not crm:
                invalid("Only a CRM column can project a Task field", column_id=identity)
            agreed = record_field_type(field, definitions)
            if agreed is not None and column["type"] != agreed:
                invalid(f"Column {identity} projects {field}, which is {agreed}", column_id=identity)
        result.append(dict(column))
    return result


def bindings_checked(before, after):
    """A column keeps being local or projected; changing that would strand or fabricate values."""
    old = {column["id"]: column.get("record_field") for column in before}
    for column in after:
        if column["id"] in old and old[column["id"]] != column.get("record_field"):
            invalid("A column keeps its Task-field binding; add a new column instead", column_id=column["id"])


def local_ids(columns):
    return {column["id"] for column in columns if column.get("record_field") is None}
