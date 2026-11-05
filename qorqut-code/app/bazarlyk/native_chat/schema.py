"""The typed host metadata contribution of native-session Chat.

Seven tables of durable *metadata*: session bindings, per-agent counters, capture/index cursors, input requests and their
receipts, the append-only input event ledger, reply completions and owner read boundaries. None of them is a message-body
authority: the transcript archive owns conversation content, an input row keeps its text only while the input is not yet
visible in the native conversation, and a reply row keeps a derived notification preview that the archive can rebuild.

The contribution is registered in ``composition.assembly``; the generated retained-domain schema is not touched.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "native-chat"

_SESSIONS_IMMUTABLE = frozenset({"id", "agent_id", "provider", "conversation_id", "generation", "transport", "process_pid",
                                 "process_started", "owner", "observed_at"})
_INPUTS_IMMUTABLE = frozenset({"id", "agent_id", "session_id", "origin", "input_type", "source_key", "attempt", "request_hash",
                               "principal_id", "sensitive", "steer", "marker", "sequence", "created_at"})
_CAPTURES_IMMUTABLE = frozenset({"agent_id", "provider", "conversation_id", "transcript_path", "created_at"})

TABLE_SPECS = {
    "native_agent_counters": TableSpec(("agent_id",), revision_column=None),
    "native_sessions": TableSpec(("id",), revision_column=None),
    "native_captures": TableSpec(("agent_id", "provider", "conversation_id"), revision_column=None),
    "native_inputs": TableSpec(("id",), revision_column=None),
    "native_input_events": TableSpec(("id",), mutable=False, revision_column=None),
    "native_replies": TableSpec(("id",), mutable=False, revision_column=None),
    "native_reads": TableSpec(("agent_id", "principal_id"), revision_column=None),
}

IMMUTABLE_FIELDS = {
    "native_sessions": _SESSIONS_IMMUTABLE,
    "native_inputs": _INPUTS_IMMUTABLE,
    "native_captures": _CAPTURES_IMMUTABLE,
}

SCHEMA = (
    """CREATE TABLE native_agent_counters (
    agent_id TEXT PRIMARY KEY REFERENCES principals(id),
    session_generation INTEGER NOT NULL DEFAULT 0 CHECK(session_generation >= 0),
    completion_sequence INTEGER NOT NULL DEFAULT 0 CHECK(completion_sequence >= 0),
    input_sequence INTEGER NOT NULL DEFAULT 0 CHECK(input_sequence >= 0),
    work_sequence INTEGER NOT NULL DEFAULT 0 CHECK(work_sequence >= 0),
    user_sequence INTEGER NOT NULL DEFAULT 0 CHECK(user_sequence >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    updated_at TEXT NOT NULL
) STRICT""",
    """CREATE TABLE native_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES principals(id),
    provider TEXT NOT NULL CHECK(provider IN ('claude','codex')),
    conversation_id TEXT NOT NULL CHECK(length(conversation_id) BETWEEN 1 AND 256),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    transport TEXT NOT NULL CHECK(transport IN ('terminal','codex_queue')),
    process_pid INTEGER NOT NULL CHECK(process_pid > 0),
    process_started TEXT NOT NULL CHECK(length(process_started) BETWEEN 1 AND 40),
    owner TEXT CHECK(owner IS NULL OR length(owner) <= 200),
    transcript_path TEXT CHECK(transcript_path IS NULL OR length(transcript_path) BETWEEN 1 AND 4096),
    observed_at TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT CHECK(end_reason IS NULL OR length(end_reason) <= 120),
    final_captured_at TEXT,
    UNIQUE(agent_id, generation)
) STRICT""",
    """CREATE TABLE native_captures (
    agent_id TEXT NOT NULL REFERENCES principals(id),
    provider TEXT NOT NULL CHECK(provider IN ('claude','codex')),
    conversation_id TEXT NOT NULL CHECK(length(conversation_id) BETWEEN 1 AND 256),
    transcript_path TEXT NOT NULL CHECK(length(transcript_path) BETWEEN 1 AND 4096),
    session_id TEXT REFERENCES native_sessions(id),
    receipt_id TEXT,
    receipt_sequence INTEGER NOT NULL DEFAULT 0 CHECK(receipt_sequence >= 0),
    captured_length INTEGER NOT NULL DEFAULT 0 CHECK(captured_length >= 0),
    indexed_offset INTEGER NOT NULL DEFAULT 0 CHECK(indexed_offset >= 0),
    index_state_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(index_state_json)),
    complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)),
    finalized INTEGER NOT NULL DEFAULT 0 CHECK(finalized IN (0,1)),
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) <= 120),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, provider, conversation_id)
) STRICT""",
    """CREATE TABLE native_inputs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES principals(id),
    session_id TEXT NOT NULL REFERENCES native_sessions(id),
    origin TEXT NOT NULL CHECK(origin IN ('owner','system')),
    input_type TEXT NOT NULL CHECK(input_type IN ('chat','telegram','broadcast','direct_message','inbox_nudge','bootstrap_recovery')),
    source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 200),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
    principal_id TEXT REFERENCES principals(id),
    body TEXT CHECK(body IS NULL OR length(body) <= 70000),
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    steer INTEGER NOT NULL DEFAULT 0 CHECK(steer IN (0,1)),
    marker TEXT CHECK(marker IS NULL OR length(marker) BETWEEN 8 AND 120),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
    state TEXT NOT NULL CHECK(state IN ('waiting','submitting','submitted','uncertain','failed','undelivered')),
    reason TEXT CHECK(reason IS NULL OR length(reason) <= 200),
    evidence_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(evidence_json)),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settled_at TEXT,
    CHECK((origin = 'system') = (input_type IN ('inbox_nudge','bootstrap_recovery'))),
    CHECK(sensitive = 0 OR body IS NULL),
    CHECK(marker IS NULL OR origin = 'system'),
    UNIQUE(agent_id, source_key, attempt),
    UNIQUE(agent_id, sequence)
) STRICT""",
    """CREATE UNIQUE INDEX native_inputs_marker ON native_inputs(marker) WHERE marker IS NOT NULL""",
    """CREATE INDEX native_inputs_queue ON native_inputs(agent_id, state, sequence)""",
    """CREATE INDEX native_inputs_session ON native_inputs(session_id, state)""",
    """CREATE TABLE native_input_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    input_id TEXT NOT NULL REFERENCES native_inputs(id),
    state TEXT NOT NULL CHECK(state IN ('waiting','submitting','submitted','uncertain','failed','undelivered')),
    reason TEXT CHECK(reason IS NULL OR length(reason) <= 200),
    evidence_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(evidence_json)),
    created_at TEXT NOT NULL
) STRICT""",
    """CREATE INDEX native_input_events_input ON native_input_events(input_id, id)""",
    """CREATE TABLE native_replies (
    id TEXT PRIMARY KEY CHECK(length(id) = 64),
    agent_id TEXT NOT NULL REFERENCES principals(id),
    provider TEXT NOT NULL CHECK(provider IN ('claude','codex')),
    conversation_id TEXT NOT NULL CHECK(length(conversation_id) BETWEEN 1 AND 256),
    archive_id TEXT NOT NULL CHECK(length(archive_id) BETWEEN 1 AND 400),
    identity_kind TEXT NOT NULL CHECK(identity_kind IN ('native_id','raw_range')),
    identity_key TEXT NOT NULL CHECK(length(identity_key) BETWEEN 1 AND 400),
    completion_sequence INTEGER NOT NULL CHECK(completion_sequence >= 1),
    session_id TEXT REFERENCES native_sessions(id),
    receipt_id TEXT NOT NULL,
    raw_offset INTEGER NOT NULL CHECK(raw_offset >= 0),
    raw_length INTEGER NOT NULL CHECK(raw_length > 0),
    completed_at TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    preview TEXT NOT NULL CHECK(length(preview) <= 180),
    origin TEXT NOT NULL DEFAULT 'unknown' CHECK(origin IN ('user','nudge','system','unknown')),
    user_work INTEGER NOT NULL DEFAULT 0 CHECK(user_work IN (0,1)),
    UNIQUE(agent_id, provider, conversation_id, archive_id, identity_kind, identity_key),
    UNIQUE(agent_id, completion_sequence)
) STRICT""",
    """CREATE INDEX native_replies_agent ON native_replies(agent_id, completion_sequence)""",
    """CREATE INDEX native_replies_position ON native_replies(agent_id, provider, conversation_id, raw_offset)""",
    """CREATE TABLE native_reads (
    agent_id TEXT NOT NULL REFERENCES principals(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    last_read_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_read_sequence >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, principal_id)
) STRICT""",
    """CREATE TRIGGER native_agent_counters_forward BEFORE UPDATE ON native_agent_counters
WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.session_generation < OLD.session_generation
  OR NEW.completion_sequence < OLD.completion_sequence OR NEW.input_sequence < OLD.input_sequence
  OR NEW.work_sequence < OLD.work_sequence OR NEW.user_sequence < OLD.user_sequence
  OR NEW.revision < OLD.revision
BEGIN
    SELECT RAISE(ABORT, 'Native chat counters only move forward');
END""",
    """CREATE TRIGGER native_agent_counters_no_delete BEFORE DELETE ON native_agent_counters
BEGIN
    SELECT RAISE(ABORT, 'Native chat counters are never deleted');
END""",
    """CREATE TRIGGER native_sessions_identity BEFORE UPDATE ON native_sessions
WHEN NEW.id IS NOT OLD.id OR NEW.agent_id IS NOT OLD.agent_id OR NEW.provider IS NOT OLD.provider
  OR NEW.conversation_id IS NOT OLD.conversation_id OR NEW.generation IS NOT OLD.generation
  OR NEW.transport IS NOT OLD.transport OR NEW.process_pid IS NOT OLD.process_pid
  OR NEW.process_started IS NOT OLD.process_started OR NEW.owner IS NOT OLD.owner
  OR (OLD.transcript_path IS NOT NULL AND NEW.transcript_path IS NOT OLD.transcript_path) OR NEW.observed_at IS NOT OLD.observed_at
  OR (OLD.ended_at IS NOT NULL AND (NEW.ended_at IS NOT OLD.ended_at OR NEW.end_reason IS NOT OLD.end_reason))
  OR (OLD.final_captured_at IS NOT NULL AND NEW.final_captured_at IS NOT OLD.final_captured_at)
BEGIN
    SELECT RAISE(ABORT, 'A native session binding keeps its identity and its end');
END""",
    """CREATE TRIGGER native_sessions_no_delete BEFORE DELETE ON native_sessions
BEGIN
    SELECT RAISE(ABORT, 'Native session bindings are never deleted');
END""",
    """CREATE TRIGGER native_captures_forward BEFORE UPDATE ON native_captures
WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.provider IS NOT OLD.provider OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.transcript_path IS NOT OLD.transcript_path OR NEW.created_at IS NOT OLD.created_at
  OR NEW.receipt_sequence < OLD.receipt_sequence OR NEW.captured_length < OLD.captured_length
  OR NEW.indexed_offset < OLD.indexed_offset OR (OLD.finalized = 1 AND NEW.finalized = 0)
BEGIN
    SELECT RAISE(ABORT, 'A native capture cursor only moves forward');
END""",
    """CREATE TRIGGER native_captures_no_delete BEFORE DELETE ON native_captures
BEGIN
    SELECT RAISE(ABORT, 'Native capture cursors are never deleted');
END""",
    """CREATE TRIGGER native_inputs_guard BEFORE UPDATE ON native_inputs
WHEN NEW.id IS NOT OLD.id OR NEW.agent_id IS NOT OLD.agent_id OR NEW.session_id IS NOT OLD.session_id
  OR NEW.origin IS NOT OLD.origin OR NEW.input_type IS NOT OLD.input_type OR NEW.source_key IS NOT OLD.source_key
  OR NEW.attempt IS NOT OLD.attempt OR NEW.request_hash IS NOT OLD.request_hash OR NEW.principal_id IS NOT OLD.principal_id
  OR NEW.sensitive IS NOT OLD.sensitive OR NEW.steer IS NOT OLD.steer OR NEW.marker IS NOT OLD.marker OR NEW.sequence IS NOT OLD.sequence
  OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.state IN ('submitted','uncertain','failed','undelivered') AND NEW.state IS NOT OLD.state)
  OR (OLD.state = 'waiting' AND NEW.state NOT IN ('waiting','submitting','failed','undelivered'))
  OR (OLD.state = 'submitting' AND NEW.state = 'undelivered')
BEGIN
    SELECT RAISE(ABORT, 'A native input keeps its identity and never leaves a final state');
END""",
    """CREATE TRIGGER native_inputs_no_delete BEFORE DELETE ON native_inputs
BEGIN
    SELECT RAISE(ABORT, 'Native inputs are never deleted');
END""",
    """CREATE TRIGGER native_input_events_no_update BEFORE UPDATE ON native_input_events
BEGIN
    SELECT RAISE(ABORT, 'The native input ledger is append-only');
END""",
    """CREATE TRIGGER native_input_events_no_delete BEFORE DELETE ON native_input_events
BEGIN
    SELECT RAISE(ABORT, 'The native input ledger is append-only');
END""",
    """CREATE TRIGGER native_replies_no_update BEFORE UPDATE ON native_replies
BEGIN
    SELECT RAISE(ABORT, 'A reply completion is never changed');
END""",
    """CREATE TRIGGER native_replies_no_delete BEFORE DELETE ON native_replies
BEGIN
    SELECT RAISE(ABORT, 'A reply completion is never deleted');
END""",
    """CREATE TRIGGER native_reads_boundary_insert BEFORE INSERT ON native_reads
WHEN NEW.last_read_sequence > 0 AND NOT EXISTS (
    SELECT 1 FROM native_replies WHERE agent_id = NEW.agent_id AND completion_sequence = NEW.last_read_sequence)
BEGIN
    SELECT RAISE(ABORT, 'A read boundary must be an existing completion');
END""",
    """CREATE TRIGGER native_reads_boundary_update BEFORE UPDATE ON native_reads
WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.principal_id IS NOT OLD.principal_id
  OR NEW.last_read_sequence < OLD.last_read_sequence
  OR (NEW.last_read_sequence > 0 AND NOT EXISTS (
      SELECT 1 FROM native_replies WHERE agent_id = NEW.agent_id AND completion_sequence = NEW.last_read_sequence))
BEGIN
    SELECT RAISE(ABORT, 'A read boundary only moves forward to an existing completion');
END""",
    """CREATE TRIGGER native_reads_no_delete BEFORE DELETE ON native_reads
BEGIN
    SELECT RAISE(ABORT, 'Read boundaries are never deleted');
END""",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, TABLE_SPECS, IMMUTABLE_FIELDS, SCHEMA)
