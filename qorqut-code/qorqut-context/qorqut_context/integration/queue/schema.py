"""The one durable Context input queue, composed once into the operational schema.

Accepted input, attempt and control history is append-only. Only processing state and receipts change, a published or
discarded input is final, and every state change must be one of the legal transitions. GENERAL source changes and
accepted Action revisions share the single ``sequence`` order; each input is owned by exactly one namespace
(``business_id`` NULL is Global). The donor layout is kept: the same states, the exact reserved Context event
identity and the same immutable payload, with ownership added and the donor's references to its own item/agent
tables pointed at this schema (``action_revisions`` and ``principals``).
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

STATES = ("queued", "processing", "blocked", "published", "discard_requested", "discarded")
EVENT_ID = "context_event_id IS NULL OR (length(context_event_id) = 5 AND context_event_id NOT GLOB '*[^a-z0-9]*')"

SCHEMA = (
    f"""CREATE TABLE context_inputs (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE CHECK (length(id) = 36),
    business_id TEXT REFERENCES businesses(id),
    input_kind TEXT NOT NULL CHECK (input_kind IN ('general','action')),
    item_id TEXT,
    revision INTEGER,
    operation TEXT NOT NULL CHECK (operation IN ('add','edit','remove')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    submitted_by TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','processing','blocked','published','discard_requested','discarded')),
    context_event_id TEXT UNIQUE CHECK ({EVENT_ID}),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    UNIQUE (item_id, revision),
    CHECK ((input_kind = 'action' AND item_id IS NOT NULL AND revision IS NOT NULL)
        OR (input_kind = 'general' AND item_id IS NULL AND revision IS NULL)),
    CHECK (state IN ('published','discarded') OR result_json IS NULL),
    CHECK (state != 'published' OR context_event_id IS NOT NULL),
    FOREIGN KEY (item_id, revision) REFERENCES action_revisions(action_key, revision)
)""",
    """CREATE INDEX context_inputs_by_state ON context_inputs(state, sequence)""",
    """CREATE INDEX context_inputs_by_scope ON context_inputs(business_id, sequence)""",
    """CREATE UNIQUE INDEX context_inputs_single_open_head ON context_inputs((1))
    WHERE state IN ('processing','blocked','discard_requested')""",
    """CREATE TRIGGER context_inputs_action_revision
BEFORE INSERT ON context_inputs
WHEN NEW.input_kind = 'action' AND NOT EXISTS (
    SELECT 1 FROM action_revisions r
    WHERE r.action_key = NEW.item_id AND r.revision = NEW.revision AND r.business_id IS NEW.business_id
      AND r.operation = NEW.operation AND r.recorded_by = NEW.submitted_by)
BEGIN
    SELECT RAISE(ABORT, 'An Action input must snapshot its stored revision in the same namespace');
END""",
    """CREATE TRIGGER context_inputs_immutable_input
BEFORE UPDATE OF sequence, id, business_id, input_kind, item_id, revision, operation, payload_json, submitted_by, created_at
ON context_inputs
BEGIN
    SELECT RAISE(ABORT, 'Context input is immutable');
END""",
    """CREATE TRIGGER context_inputs_final
BEFORE UPDATE ON context_inputs
WHEN OLD.state IN ('published','discarded')
BEGIN
    SELECT RAISE(ABORT, 'A published or discarded Context input is final');
END""",
    """CREATE TRIGGER context_inputs_legal_transition
BEFORE UPDATE OF state ON context_inputs
WHEN NEW.state IS NOT OLD.state AND NOT (
       (OLD.state = 'queued' AND NEW.state IN ('processing','blocked','discarded'))
    OR (OLD.state = 'processing' AND NEW.state IN ('published','blocked'))
    OR (OLD.state = 'blocked' AND NEW.state IN ('processing','discard_requested'))
    OR (OLD.state = 'discard_requested' AND NEW.state IN ('discarded','published')))
BEGIN
    SELECT RAISE(ABORT, 'Illegal Context input state transition');
END""",
    """CREATE TRIGGER context_inputs_keep_history
BEFORE DELETE ON context_inputs
BEGIN
    SELECT RAISE(ABORT, 'Context input history is retained');
END""",
    """CREATE TABLE context_input_controls (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE CHECK (length(id) = 36),
    business_id TEXT REFERENCES businesses(id),
    input_id TEXT NOT NULL REFERENCES context_inputs(id),
    intent TEXT NOT NULL CHECK (intent IN ('retry','discard')),
    expected_state TEXT NOT NULL
        CHECK (expected_state IN ('queued','processing','blocked','published','discard_requested','discarded')),
    requested_by TEXT NOT NULL REFERENCES principals(id),
    requested_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','applied','rejected')),
    resolved_at TEXT,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    CHECK ((status = 'pending') = (resolved_at IS NULL))
)""",
    """CREATE UNIQUE INDEX context_input_one_pending_control ON context_input_controls(input_id)
    WHERE status = 'pending'""",
    """CREATE TRIGGER context_input_controls_scope
BEFORE INSERT ON context_input_controls
WHEN NEW.business_id IS NOT (SELECT business_id FROM context_inputs WHERE id = NEW.input_id)
BEGIN
    SELECT RAISE(ABORT, 'A control request belongs to its input''s namespace');
END""",
    """CREATE TRIGGER context_input_controls_immutable_request
BEFORE UPDATE OF sequence, id, business_id, input_id, intent, expected_state, requested_by, requested_at
ON context_input_controls
BEGIN
    SELECT RAISE(ABORT, 'Context control request is immutable');
END""",
    """CREATE TRIGGER context_input_controls_resolve_once
BEFORE UPDATE ON context_input_controls
WHEN OLD.status != 'pending'
BEGIN
    SELECT RAISE(ABORT, 'A resolved Context control request is final');
END""",
    """CREATE TRIGGER context_input_controls_keep_history
BEFORE DELETE ON context_input_controls
BEGIN
    SELECT RAISE(ABORT, 'Context control history is retained');
END""",
    f"""CREATE TABLE context_input_attempts (
    input_id TEXT NOT NULL REFERENCES context_inputs(id),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    context_event_id TEXT CHECK ({EVENT_ID}),
    cause TEXT NOT NULL CHECK (cause IN ('queue','retry')),
    control_id TEXT REFERENCES context_input_controls(id),
    started_at TEXT NOT NULL,
    outcome TEXT CHECK (outcome IN ('published','blocked')),
    finished_at TEXT,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    PRIMARY KEY (input_id, attempt),
    CHECK ((outcome IS NULL) = (finished_at IS NULL)),
    CHECK ((cause = 'retry') = (control_id IS NOT NULL)),
    CHECK (context_event_id IS NOT NULL OR COALESCE(outcome, '') = 'blocked')
)""",
    """CREATE TRIGGER context_input_attempts_immutable_start
BEFORE UPDATE OF input_id, attempt, context_event_id, cause, control_id, started_at
ON context_input_attempts
BEGIN
    SELECT RAISE(ABORT, 'Context attempt start is immutable');
END""",
    """CREATE TRIGGER context_input_attempts_outcome_once
BEFORE UPDATE ON context_input_attempts
WHEN OLD.outcome IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'A finished Context attempt is final');
END""",
    """CREATE TRIGGER context_input_attempts_keep_history
BEFORE DELETE ON context_input_attempts
BEGIN
    SELECT RAISE(ABORT, 'Context attempt history is retained');
END""",
)

TABLES = {
    "context_inputs": TableSpec(("id",), "business_id", True, None),
    "context_input_controls": TableSpec(("id",), "business_id", False, None),
    "context_input_attempts": TableSpec(("input_id", "attempt"), mutable=False, revision_column=None),
}
IMMUTABLE_FIELDS = {
    "context_inputs": frozenset({"sequence", "input_kind", "item_id", "revision", "operation", "payload_json", "submitted_by"}),
}
# Action inputs reference the exact accepted revision; the queue reads it to freeze its payload.
REQUIRED_TABLES = frozenset({"action_revisions"})


def storage_contribution() -> StorageContribution:
    return StorageContribution("context-queue", TABLES, IMMUTABLE_FIELDS, SCHEMA, required_tables=REQUIRED_TABLES)
