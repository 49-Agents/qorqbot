"""JSON Schemas for authored files, plus schema-checking helpers.

``MANIFEST_SCHEMA``, ``TOOL_DEFINITION_SCHEMA`` and ``WORKFLOW_SCHEMA`` describe
the files authors write. They are documented in docs/FILE-FORMATS.md.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .util import ID_PATTERN, REF_PATTERN

ENTRY_KINDS = ("skill", "instruction", "preference", "workflow", "tool", "template")
TEXT_REF_KINDS = ("instruction", "skill", "preference", "template")
ACQUISITION_MODES = ("default", "ask_before_start", "ask_at_step", "confirm_default", "derive")
ALIAS_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

_ID = {"type": "string", "pattern": ID_PATTERN}
_REF = {"type": "string", "pattern": REF_PATTERN}
_TEXT = {"type": "string", "minLength": 1}
_STRINGS = {"type": "array", "items": _TEXT, "uniqueItems": True, "maxItems": 200}
_JSON_SCHEMA = {"type": ["object", "boolean"]}

MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "agentic.yaml",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "id", "name", "description", "entries"],
    "properties": {
        "schema_version": {"const": 1},
        "id": _ID,
        "name": _TEXT,
        "description": {"type": "string"},
        "source": {"type": "object"},
        "native": {"type": "object"},
        "entries": {"type": "array", "items": {"$ref": "#/$defs/entry"}},
    },
    "$defs": {
        "entry": {
            "type": "object",
            "additionalProperties": False,
            "required": ["id", "kind", "description", "when_to_use", "path"],
            "properties": {
                "id": _ID,
                "kind": {"enum": list(ENTRY_KINDS)},
                "description": _TEXT,
                "when_to_use": _TEXT,
                "path": _TEXT,
                "requires": {"type": "array", "items": _REF, "uniqueItems": True},
                "applicability": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"work_types": _STRINGS, "project_ids": _STRINGS},
                },
                "native": {"type": "object"},
            },
        }
    },
}

TOOL_DEFINITION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "tool definition",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "runtime", "code", "input_schema", "output_schema"],
    "properties": {
        "schema_version": {"const": 1},
        "description": {"type": "string"},
        "runtime": {"type": "string", "pattern": ALIAS_PATTERN},
        "code": _TEXT,
        "resources": {"type": "array", "items": _TEXT, "uniqueItems": True, "maxItems": 200},
        "input_schema": _JSON_SCHEMA,
        "output_schema": _JSON_SCHEMA,
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
                "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": 67108864},
                "max_stderr_bytes": {"type": "integer", "minimum": 0, "maximum": 1048576},
            },
        },
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "input", "expect"],
                "properties": {
                    "name": _TEXT,
                    "input": {},
                    "expect": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["status"],
                        "properties": {
                            "status": {"enum": ["succeeded", "failed", "rejected"]},
                            "output": {},
                            "failure_code": {"type": "string"},
                            "error_code": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}

DEFAULT_TOOL_LIMITS = {"timeout_seconds": 30, "max_output_bytes": 1048576, "max_stderr_bytes": 16384}

WORKFLOW_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "workflow definition",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "id", "description", "steps"],
    "properties": {
        "schema_version": {"const": 1},
        "id": _ID,
        "description": _TEXT,
        "arguments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "schema", "prompt", "acquisition"],
                "properties": {
                    "name": _ID,
                    "schema": _JSON_SCHEMA,
                    "default": {},
                    "prompt": _TEXT,
                    "step": _ID,
                    "acquisition": {"enum": list(ACQUISITION_MODES)},
                },
            },
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id"],
                "properties": {
                    "id": _ID,
                    "instructions": _TEXT,
                    "instruction_ref": _REF,
                    "depends_on": {"type": "array", "items": _ID, "uniqueItems": True},
                    "verifier": _REF,
                },
                "oneOf": [{"required": ["instructions"]}, {"required": ["instruction_ref"]}],
            },
        },
    },
}


def _format_path(error: Any) -> str:
    parts = [str(part) for part in error.absolute_path]
    return "/" + "/".join(parts) if parts else "(root)"


def schema_issues(instance: Any, schema: dict[str, Any], *, limit: int = 20) -> list[str]:
    """Return human-readable violations of ``schema`` (empty when valid)."""
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: (list(map(str, e.absolute_path)), e.message))
    return [f"{_format_path(error)}: {error.message}" for error in errors[:limit]]


def check_embedded_schema(schema: Any) -> str | None:
    """Return a problem with a user-supplied JSON Schema, or ``None``.

    Besides metaschema validity, remote references are refused: only in-document
    ``#...`` references are allowed so validation never reaches the network.
    """
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return exc.message
    problem = _find_remote_ref(schema)
    return problem


def _find_remote_ref(node: Any) -> str | None:
    if isinstance(node, dict):
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            value = node.get(key)
            if isinstance(value, str) and not value.startswith("#"):
                return f"{key} {value!r} is not an in-document reference"
        for value in node.values():
            found = _find_remote_ref(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_remote_ref(value)
            if found:
                return found
    return None


def validate_instance(instance: Any, schema: Any, *, limit: int = 20) -> list[str]:
    """Validate ``instance`` against a user-supplied (already checked) schema."""
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: (list(map(str, e.absolute_path)), e.message))
    return [f"{_format_path(error)}: {error.message}" for error in errors[:limit]]
