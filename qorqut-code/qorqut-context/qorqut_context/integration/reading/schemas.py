"""Request bounds and the explicit JSON Schemas of the shared Context read DTOs (Draft 2020-12).

Every response object is closed (``additionalProperties: false``) so a drifting DTO fails output validation instead
of leaking a field; the reader's tests validate real responses against these schemas."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

# (minimum, maximum, default)
HOME_LIMIT = (1, 60, 30)
HISTORY_LIMIT = (1, 25, 10)
TEXT_LIMIT = (1, 64000, 16000)
FIND_LIMIT = (1, 100, 20)
EXPAND_LIMIT = (1, 200, 50)
CHANGES_LIMIT = (1, 100, 20)
# The pinned engine's index returns at most this many hits per query, so a search can be paged this deep.
MAX_SEARCH_WINDOW = 1000

CURSOR = {"type": "string", "minLength": 1, "maxLength": 1024}
REF = {"type": "string", "pattern": "^((node|edge|source):)?[a-z0-9]{5}$"}
RECORD_REF = {"type": "string", "pattern": "^((node|edge|note|source):)?[a-z0-9]{5}$"}


def _limit(spec, description=None):
    low, high, default = spec
    schema = {"type": "integer", "minimum": low, "maximum": high, "default": default}
    return {**schema, "description": description} if description else schema


def object_schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": sorted(properties if required is None else required),
            "additionalProperties": False}


# ---- request schemas (the operation layer closes each top-level object)

HOME_INPUT = {"limit": _limit(HOME_LIMIT, instruction_text('schemas-nodes-per-page-most-connected-first')), "cursor": CURSOR}
OBJECT_INPUT = {
    "ref": {**RECORD_REF,
            "description": instruction_text('schemas-node-id-edge-id-note-id-source-id')},
    "history_limit": _limit(HISTORY_LIMIT), "history_cursor": CURSOR,
    "text_limit": _limit(TEXT_LIMIT, instruction_text('schemas-characters-of-a-source-s-committed-text-per')), "text_cursor": CURSOR,
}
FIND_INPUT = {
    "query": {"type": "string", "maxLength": 2000, "default": ""},
    "kind": {"enum": ["node", "edge", "note", "source"]},
    "attributes": {"type": "object", "additionalProperties": {
        "type": "array", "items": {"type": ["string", "number", "boolean"]}, "minItems": 1}},
    "limit": _limit(FIND_LIMIT), "cursor": CURSOR,
}
EXPAND_INPUT = {
    "ref": {**REF, "description": instruction_text('schemas-a-node-reference')},
    "depth": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1},
    "direction": {"enum": ["out", "in", "both"], "default": "both"},
    "limit": _limit(EXPAND_LIMIT), "cursor": CURSOR,
}
CHANGES_INPUT = {"ref": RECORD_REF, "limit": _limit(CHANGES_LIMIT), "cursor": CURSOR}

# ---- response definitions

_STRING = {"type": "string"}
_NULLABLE = {"type": ["string", "null"]}
_COMMIT = {"type": "string", "pattern": "^([0-9a-f]{40}|[0-9a-f]{64})$"}
_ID = {"type": "string", "pattern": "^[a-z0-9]{5}$"}
_MODE = {"enum": ["GENERAL", "OPERATIONAL_ACTION"]}
_ATTRIBUTES = {"type": "object", "additionalProperties": {
    "type": "array", "items": {"type": ["string", "number", "boolean"]}}}
_COMMITS = {"type": "array", "items": _COMMIT}
_REF = lambda kind: {"type": "string", "pattern": f"^{kind}:[a-z0-9]{{5}}$"}  # noqa: E731
_SUBJECT_REF = "^(node|edge):[a-z0-9]{5}$"


def _ref(name):
    return {"$ref": f"#/$defs/{name}"}


DEFINITIONS = {
    "endpoint": object_schema({"kind": {"const": "node"}, "id": _ID, "ref": _REF("node"), "summary": _NULLABLE,
                               "truncated": {"type": "boolean"}}),
    "node": object_schema({"kind": {"const": "node"}, "id": _ID, "ref": _REF("node"), "summary": _STRING,
                           "attributes": _ATTRIBUTES, "truncated": {"type": "boolean"}}),
    "relationship": object_schema({
        "kind": {"const": "edge"}, "id": _ID, "ref": _REF("edge"), "summary": _STRING, "attributes": _ATTRIBUTES,
        "truncated": {"type": "boolean"}, "from": _ref("endpoint"), "to": _ref("endpoint"),
        "direction": {"enum": ["out", "in", "self"]}, "depth": {"type": "integer", "minimum": 1}},
        required=["kind", "id", "ref", "summary", "attributes", "truncated", "from", "to"]),
    "note": object_schema({"kind": {"const": "note"}, "id": _ID, "ref": _REF("note"), "text": _STRING,
                           "truncated": {"type": "boolean"}, "subject_count": {"type": "integer", "minimum": 0}}),
    "source_summary": object_schema({"id": _ID, "ref": _REF("source"), "title": _STRING, "source_kind": _STRING,
                                     "origin": _STRING, "origin_ref": _NULLABLE, "created_at": _NULLABLE}),
    "source": object_schema({
        "kind": {"const": "source"}, "id": _ID, "ref": _REF("source"), "title": _STRING, "source_kind": _STRING,
        "origin": _STRING, "origin_ref": _NULLABLE, "created_at": _NULLABLE, "input_mode": _MODE,
        "withdrawn": {"type": "boolean"}, "excerpt": _NULLABLE, "truncated": {"type": "boolean"}}),
    "home_node": object_schema({
        "kind": {"const": "node"}, "id": _ID, "ref": _REF("node"), "summary": _STRING, "attributes": _ATTRIBUTES,
        "truncated": {"type": "boolean"}, "relationship_count": {"type": "integer", "minimum": 0},
        "relationships": {"type": "array", "items": _ref("relationship")},
        "relationships_truncated": {"type": "boolean"}}),
    "node_record": object_schema({"kind": {"const": "node"}, "id": _ID, "ref": _REF("node"), "summary": _STRING,
                                  "attributes": _ATTRIBUTES}),
    "edge_record": object_schema({"kind": {"const": "edge"}, "id": _ID, "ref": _REF("edge"), "summary": _STRING,
                                  "attributes": _ATTRIBUTES, "from": _ref("endpoint"), "to": _ref("endpoint")}),
    "note_record": object_schema({"kind": {"const": "note"}, "id": _ID, "ref": _REF("note"), "text": _STRING}),
    "subject": object_schema({
        "kind": {"enum": ["node", "edge"]}, "id": _ID, "ref": {"type": "string", "pattern": _SUBJECT_REF},
        "status": {"enum": ["current", "removed"]}, "summary": _NULLABLE, "truncated": {"type": "boolean"}}),
    "subjects": object_schema({"count": {"type": "integer", "minimum": 0},
                               "items": {"type": "array", "items": _ref("subject")}, "truncated": {"type": "boolean"}}),
    "source_record": object_schema({
        "kind": {"const": "source"}, "id": _ID, "ref": _REF("source"), "title": _STRING, "source_kind": _STRING,
        "origin": _STRING, "origin_ref": _NULLABLE, "created_at": _NULLABLE, "input_mode": _MODE,
        "operation": {"enum": ["add", "edit", "remove"]}, "withdrawn": {"type": "boolean"}}),
    "evidence": object_schema({"commit": _COMMIT, "event_id": _ID, "recorded_at": _STRING, "input_mode": _MODE,
                               "source": _ref("source_summary")}),
    "support": object_schema({"count": {"type": "integer", "minimum": 0}, "items": {"type": "array", "items": _ref("evidence")},
                              "truncated": {"type": "boolean"}}),
    "supports": object_schema({"count": {"type": "integer", "minimum": 0},
                               "items": {"type": "array", "items": {
                                   "oneOf": [_ref("node"), _ref("relationship"), _ref("note")]}},
                               "truncated": {"type": "boolean"}}),
    "stored_graph": {"type": "object", "properties": {
        "kind": {"enum": ["node", "edge"]}, "id": _ID, "ref": {"type": "string"}, "summary": _STRING,
        "attributes": _ATTRIBUTES, "from_id": _ID, "to_id": _ID},
        "required": ["kind", "id", "ref", "summary", "attributes"], "additionalProperties": False},
    "stored_note": object_schema({
        "kind": {"const": "note"}, "id": _ID, "ref": _REF("note"), "text": _STRING,
        "objects": {"type": "array", "items": object_schema({
            "kind": {"enum": ["node", "edge"]}, "id": _ID, "ref": {"type": "string", "pattern": _SUBJECT_REF}})}}),
    "stored": {"oneOf": [{"type": "null"}, _ref("stored_graph"), _ref("stored_note")]},
    "development": object_schema({"date": {"type": ["string", "integer"]}, "description": _STRING, "citations": _COMMITS}),
    "history_entry": object_schema({
        "event_id": _ID, "commit": _COMMIT, "recorded_at": _STRING, "input_mode": _MODE,
        "operation": {"enum": ["add", "edit", "remove"]}, "explanation": _STRING, "citations": _COMMITS,
        "support_after": _COMMITS, "developments": {"type": "array", "items": _ref("development")},
        "source": _ref("source_summary"), "before": _ref("stored"), "after": _ref("stored")}),
    "source_history_entry": object_schema({
        "event_id": _ID, "commit": _COMMIT, "recorded_at": _STRING, "input_mode": _MODE,
        "operation": {"enum": ["add", "edit", "remove"]}, "change_count": {"type": "integer", "minimum": 0}}),
    "page": object_schema({
        "limit": {"type": "integer", "minimum": 1}, "returned": {"type": "integer", "minimum": 0},
        "has_more": {"type": "boolean"}, "next_cursor": _NULLABLE, "window_exhausted": {"type": "boolean"}},
        required=["limit", "returned", "has_more", "next_cursor"]),
    "change_event": object_schema({
        "event_id": _ID, "commit": _COMMIT, "recorded_at": _STRING, "input_mode": _MODE,
        "operation": {"enum": ["add", "edit", "remove"]}, "submitted_by": _STRING,
        "source": object_schema({"id": _ID, "ref": _REF("source"), "title": _STRING, "source_kind": _STRING,
                                 "origin": _STRING, "origin_ref": _NULLABLE, "created_at": _NULLABLE, "path": _STRING}),
        "changes": {"type": "array", "items": object_schema({
            "kind": {"enum": ["node", "edge", "note"]}, "id": _ID, "ref": {"type": "string"},
            "operation": {"enum": ["add", "edit", "remove"]}, "explanation": _STRING, "citations": _COMMITS})},
        "change_count": {"type": "integer", "minimum": 0}, "changes_truncated": {"type": "boolean"},
        "handoff_disposition": {"enum": ["prepared", "empty", None]},
        "action": {"oneOf": [{"type": "null"}, object_schema({
            "action_ref": _STRING, "revision_ref": _NULLABLE,
            "tags": {"type": "array", "items": object_schema({"id": _STRING, "name": _STRING, "business_id": _NULLABLE})}})]}}),
    "unavailable": object_schema({
        "status": {"enum": ["not_initialized", "unavailable"]}, "revision": {"type": "null"},
        "reason": object_schema({"code": _STRING, "message": _STRING})}),
}


def _ready(properties):
    return {"properties": {"status": {"const": "ready"}, "revision": _COMMIT, **properties},
            "required": ["status", "revision", *properties], "additionalProperties": False, "type": "object"}


def _output(properties):
    return {"$defs": DEFINITIONS, "oneOf": [_ready(properties), _ref("unavailable")]}


_HISTORY = {"type": "object", "properties": {"total": {"type": "integer", "minimum": 0}, "items": {"type": "array"},
            "page": _ref("page")}, "required": ["total", "items", "page"], "additionalProperties": False}
_TOTALS = object_schema({"nodes": {"type": "integer", "minimum": 0}, "edges": {"type": "integer", "minimum": 0},
                         "sources": {"type": "integer", "minimum": 0}, "events": {"type": "integer", "minimum": 0}})
_CONTENT = {"oneOf": [{"type": "null"}, object_schema({
    "text": _STRING, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1},
    "total_characters": {"type": "integer", "minimum": 0}, "has_more": {"type": "boolean"}, "next_cursor": _NULLABLE})]}

HOME_OUTPUT = _output({"empty": {"type": "boolean"}, "totals": _TOTALS,
                       "personal_context": {"oneOf": [{"type": "null"}, object_schema({"ref": _REF("node")})]},
                       "nodes": {"type": "array", "items": _ref("home_node")}, "page": _ref("page")})
FIND_OUTPUT = _output({"query": _STRING, "items": {"type": "array", "items": {
    "oneOf": [_ref("node"), _ref("relationship"), _ref("note"), _ref("source")]}}, "page": _ref("page")})
EXPAND_OUTPUT = _output({"origin": _ref("node"), "depth": {"type": "integer", "minimum": 1, "maximum": 3},
                         "direction": {"enum": ["out", "in", "both"]}, "total": {"type": "integer", "minimum": 0},
                         "relationships": {"type": "array", "items": _ref("relationship")}, "page": _ref("page")})
CHANGES_OUTPUT = _output({"total": {"type": "integer", "minimum": 0},
                          "items": {"type": "array", "items": _ref("change_event")}, "page": _ref("page")})


def _object_output():
    history_node = {**_HISTORY, "properties": {**_HISTORY["properties"],
                    "items": {"type": "array", "items": _ref("history_entry")}}}
    history_source = {**_HISTORY, "properties": {**_HISTORY["properties"],
                      "items": {"type": "array", "items": _ref("source_history_entry")}}}
    node = _ready({"object": _ref("node_record"), "relationship_count": {"type": "integer", "minimum": 0},
                   "relationships": {"type": "array", "items": _ref("relationship")},
                   "relationships_truncated": {"type": "boolean"}, "support": _ref("support"), "history": history_node})
    edge = _ready({"object": _ref("edge_record"), "support": _ref("support"), "history": history_node})
    note = _ready({"object": _ref("note_record"), "subjects": _ref("subjects"), "support": _ref("support"),
                   "history": history_node})
    source = _ready({"object": _ref("source_record"), "content": _CONTENT, "supports": _ref("supports"),
                     "history": history_source})
    return {"$defs": DEFINITIONS, "oneOf": [node, edge, note, source, _ref("unavailable")]}


OBJECT_OUTPUT = _object_output()
