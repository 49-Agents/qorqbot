"""Durable binding of a controlled deterministic graph edit to its queue input, composed once into the operational schema.

A graph edit is not another queue or Context input mode. It is an ordinary GENERAL ``add`` input whose payload is the
canonical plan text at ``foundation/edits/{reserved_event_id}.txt`` with the fixed controlled metadata, plus exactly one
immutable companion row in ``context_graph_edits``:

* ``input_id``: the queue input (primary key and deferred foreign key; the companion is inserted first, in the same
  prepared transaction, and the input second).
* ``reserved_event_id``: the Context event ID reserved for this edit, unique across all edits and never an ID any queue
  input or attempt used. It is the only event ID the input's attempts may carry.
* ``plan_digest``, ``expected_revision``, ``plan_json``: the one authoritative normalized plan, its digest (the SHA-256
  of the canonical source bytes) and the published base it was prepared against. The payload's content must be exactly
  this plan; nothing is stored as an independently editable copy.
* ``attribution_json``: the original caller's attribution (the immutable ``submitted_by`` is the same principal). Its
  closed ``delegation`` value is ``null`` for a direct caller, otherwise exactly the authenticated ``binding_id``,
  ``assignment_id`` and positive ``generation`` of an agent principal: identities only, never a credential, lease or decision.
* ``targets_json``: the frozen target evidence the host's authority check produced at preparation: what it must
  re-derive, never a permission.

The input's immutable ``business_id`` stays its selected owning namespace. Authority is never read from this table, from
tags or from the source path: the host re-resolves the original caller's current permission each time it matters.

The database refuses a reserved-path input without its matching companion, a companion whose input is not exactly the
reserved-path ``add`` with the fixed metadata and the same submitter, a companion for an input that already exists, a
reservation of an event ID any queue input or attempt used, any attempt that carries a different event ID than the
reservation (or another edit's reservation), and every update or deletion of a binding. The SHA-256 relation between the
payload bytes and ``plan_digest`` is checked in code at admission and on every read, because SQLite has no hash function.
"""

from __future__ import annotations

from qorqut_context.integration.queue.queue import EDIT_PREFIX
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

PREFIX = EDIT_PREFIX
EXECUTION = "deterministic_graph_edit"
# The fixed source metadata of a controlled graph-edit input; a public submission cannot produce it (``origin`` is refused).
METADATA = {"kind": "graph_edit", "title": "Explicit graph edit", "origin": "controlled_caller"}
HEX = "NOT GLOB '*[^0-9a-f]*'"
HEX_DASHED = "NOT GLOB '*[^0-9a-f-]*'"

_PREFIX_LENGTH = len(PREFIX)
# Everything a reserved-path input must be, from stored columns alone.
_MATCHING_BINDING = f"""EXISTS (SELECT 1 FROM context_graph_edits g
    WHERE g.input_id = NEW.id AND NEW.input_kind = 'general' AND NEW.operation = 'add' AND NEW.state = 'queued'
      AND NEW.context_event_id IS NULL AND NEW.attempt = 0
      AND json_extract(NEW.payload_json, '$.path') = '{PREFIX}' || g.reserved_event_id || '.txt'
      AND json_extract(NEW.payload_json, '$.submitted_by') = NEW.submitted_by
      AND json_extract(g.attribution_json, '$.principal_id') = NEW.submitted_by
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json)) = 5
      AND (SELECT COUNT(*) FROM json_each(NEW.payload_json, '$.metadata')) = 3
      AND json_extract(NEW.payload_json, '$.metadata.kind') = 'graph_edit'
      AND json_extract(NEW.payload_json, '$.metadata.title') = 'Explicit graph edit'
      AND json_extract(NEW.payload_json, '$.metadata.origin') = 'controlled_caller')"""

SCHEMA = (
    f"""CREATE TABLE context_graph_edits (
    input_id TEXT PRIMARY KEY CHECK (length(input_id) = 36) REFERENCES context_inputs(id) DEFERRABLE INITIALLY DEFERRED,
    reserved_event_id TEXT NOT NULL UNIQUE
        CHECK (length(reserved_event_id) = 5 AND reserved_event_id NOT GLOB '*[^a-z0-9]*'),
    plan_digest TEXT NOT NULL CHECK (length(plan_digest) = 64 AND plan_digest {HEX}),
    expected_revision TEXT NOT NULL CHECK (length(expected_revision) IN (40, 64) AND expected_revision {HEX}),
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json) AND json_type(plan_json) = 'object'),
    attribution_json TEXT NOT NULL CHECK (json_valid(attribution_json) AND json_type(attribution_json) = 'object'),
    targets_json TEXT NOT NULL CHECK (json_valid(targets_json) AND json_type(targets_json) = 'array'),
    CHECK (json_extract(plan_json, '$.expected_revision') = expected_revision)
)""",
    # The binding is created with its own new input and an event ID nothing used. The input is inserted after it.
    """CREATE TRIGGER context_graph_edits_new_binding
BEFORE INSERT ON context_graph_edits
WHEN EXISTS (SELECT 1 FROM context_inputs i WHERE i.id = NEW.input_id)
  OR EXISTS (SELECT 1 FROM context_inputs i WHERE i.context_event_id = NEW.reserved_event_id)
  OR EXISTS (SELECT 1 FROM context_input_attempts a WHERE a.context_event_id = NEW.reserved_event_id)
BEGIN
    SELECT RAISE(ABORT, 'A graph-edit binding is created before its own new input and with an event ID nothing used');
END""",
    # The closed delegation value of the attribution, from stored columns alone (a CHECK cannot count keys).
    f"""CREATE TRIGGER context_graph_edits_delegation
BEFORE INSERT ON context_graph_edits
WHEN NOT COALESCE(json_type(NEW.attribution_json, '$.delegation') = 'null' OR (
        json_type(NEW.attribution_json, '$.delegation') = 'object'
    AND json_extract(NEW.attribution_json, '$.principal_kind') = 'agent'
    AND (SELECT COUNT(*) FROM json_each(NEW.attribution_json, '$.delegation')) = 3
    AND json_type(NEW.attribution_json, '$.delegation.binding_id') = 'text'
    AND length(json_extract(NEW.attribution_json, '$.delegation.binding_id')) = 36
    AND json_extract(NEW.attribution_json, '$.delegation.binding_id') {HEX_DASHED}
    AND json_type(NEW.attribution_json, '$.delegation.assignment_id') = 'text'
    AND length(json_extract(NEW.attribution_json, '$.delegation.assignment_id')) = 36
    AND json_extract(NEW.attribution_json, '$.delegation.assignment_id') {HEX_DASHED}
    AND json_type(NEW.attribution_json, '$.delegation.generation') = 'integer'
    AND json_extract(NEW.attribution_json, '$.delegation.generation') >= 1), 0)
BEGIN
    SELECT RAISE(ABORT, 'A graph-edit binding records a closed delegation: null, or exactly its binding, assignment and generation');
END""",
    """CREATE TRIGGER context_graph_edits_immutable
BEFORE UPDATE ON context_graph_edits
BEGIN
    SELECT RAISE(ABORT, 'A graph-edit binding is immutable');
END""",
    """CREATE TRIGGER context_graph_edits_keep
BEFORE DELETE ON context_graph_edits
BEGIN
    SELECT RAISE(ABORT, 'Graph-edit bindings are retained');
END""",
    # The reserved path holds only the input of a real binding, and a binding holds only a reserved-path input.
    f"""CREATE TRIGGER context_graph_edits_controlled_inputs
BEFORE INSERT ON context_inputs
WHEN (substr(json_extract(NEW.payload_json, '$.path'), 1, {_PREFIX_LENGTH}) = '{PREFIX}'
        OR EXISTS (SELECT 1 FROM context_graph_edits x WHERE x.input_id = NEW.id))
  AND NOT {_MATCHING_BINDING}
BEGIN
    SELECT RAISE(ABORT, 'Controlled graph-edit paths hold only the input of their own graph-edit binding');
END""",
    # The input's attempts carry exactly the reserved event ID, and nobody else may carry it.
    """CREATE TRIGGER context_graph_edits_input_event
BEFORE UPDATE OF context_event_id ON context_inputs
WHEN NEW.context_event_id IS NOT NULL AND (
     EXISTS (SELECT 1 FROM context_graph_edits g WHERE g.reserved_event_id = NEW.context_event_id AND g.input_id != NEW.id)
  OR EXISTS (SELECT 1 FROM context_graph_edits g WHERE g.input_id = NEW.id AND g.reserved_event_id != NEW.context_event_id))
BEGIN
    SELECT RAISE(ABORT, 'A graph edit uses exactly its reserved Context event ID, which no other input may use');
END""",
    """CREATE TRIGGER context_graph_edits_attempt_event
BEFORE INSERT ON context_input_attempts
WHEN NEW.context_event_id IS NOT NULL AND (
     EXISTS (SELECT 1 FROM context_graph_edits g WHERE g.reserved_event_id = NEW.context_event_id AND g.input_id != NEW.input_id)
  OR EXISTS (SELECT 1 FROM context_graph_edits g WHERE g.input_id = NEW.input_id AND g.reserved_event_id != NEW.context_event_id))
BEGIN
    SELECT RAISE(ABORT, 'A graph-edit attempt carries exactly its reserved Context event ID, which no other input may use');
END""",
)

TABLES = {"context_graph_edits": TableSpec(("input_id",), None, False, None)}
# The binding references the input that carries it and the attempts whose event IDs it constrains.
REQUIRED_TABLES = frozenset({"context_inputs", "context_input_attempts"})


def storage_contribution() -> StorageContribution:
    return StorageContribution("context-graph-edits", TABLES, {}, SCHEMA, required_tables=REQUIRED_TABLES)
