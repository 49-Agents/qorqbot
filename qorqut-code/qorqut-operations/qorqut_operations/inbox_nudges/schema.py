"""The typed storage contribution of the inbox nudge engine: one durable need per registered agent, immutable attempts and
their append-only evidence.

The engine reads the host's inbox rows (``inbox_events`` and ``inbox_deliveries``, named as required tables) but owns only
the three tables below. None of them is a message store: a need and an attempt carry event ids, a binding and a state, never
an event summary, a body or the typed text."""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "inbox-nudges"
REQUIRED_TABLES = frozenset({"inbox_events", "inbox_deliveries"})

TABLE_SPECS = {
    "inbox_nudge_needs": TableSpec(("agent_id",), revision_column=None),
    "inbox_nudge_attempts": TableSpec(("id",), revision_column=None),
    "inbox_nudge_evidence": TableSpec(("attempt_id", "evidence_hash"), mutable=False, revision_column=None),
}

IMMUTABLE_FIELDS = {
    "inbox_nudge_needs": frozenset({"agent_id", "opened_at_first"}),
    "inbox_nudge_attempts": frozenset({"id", "agent_id", "cycle", "sequence", "kind", "binding_json", "session_key",
                                       "event_ids_json", "progress_json", "prepared_at"}),
}

SCHEMA = (
    """CREATE TABLE inbox_nudge_needs (
    agent_id TEXT PRIMARY KEY REFERENCES agent_profiles(agent_id),
    cycle INTEGER NOT NULL CHECK(cycle >= 0),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    opened_at TEXT NOT NULL,
    opened_at_first TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_sequence >= 1),
    reason TEXT CHECK(reason IS NULL OR length(reason) <= 200),
    -- Where the next bounded scan of this agent's pending deliveries resumes (an event id; 0 = from the start). A hint only:
    -- it is neither an acknowledgement nor an authorization, and every event is authorized again whenever it is read.
    scan_after INTEGER NOT NULL DEFAULT 0 CHECK(scan_after >= 0),
    -- Cycle 0 is the row of an agent whose scan has not yet found an authorized event; the first activation is cycle one.
    CHECK(cycle >= 1 OR active = 0)
) STRICT""",
    """CREATE TABLE inbox_nudge_attempts (
    id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 8 AND 120),
    agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
    cycle INTEGER NOT NULL CHECK(cycle >= 1),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('initial','new_session','follow_up')),
    binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
    session_key TEXT NOT NULL CHECK(length(session_key) = 64),
    event_ids_json TEXT NOT NULL CHECK(json_valid(event_ids_json)),
    progress_json TEXT NOT NULL CHECK(json_valid(progress_json)),
    state TEXT NOT NULL CHECK(state IN ('prepared','submitted','uncertain','deferred','failed','unavailable','stale')),
    reason TEXT CHECK(reason IS NULL OR length(reason) <= 200),
    prepared_at TEXT NOT NULL,
    settled_at TEXT,
    outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
    UNIQUE(agent_id, sequence)
) STRICT""",
    """CREATE INDEX inbox_nudge_attempts_cycle ON inbox_nudge_attempts(agent_id, cycle, sequence)""",
    # One reservation at a time per agent: a second prepare cannot slip in while one is unsettled.
    """CREATE UNIQUE INDEX inbox_nudge_attempts_reserved ON inbox_nudge_attempts(agent_id) WHERE state = 'prepared'""",
    """CREATE TABLE inbox_nudge_evidence (
    attempt_id TEXT NOT NULL REFERENCES inbox_nudge_attempts(id),
    evidence_hash TEXT NOT NULL CHECK(length(evidence_hash) = 64),
    state TEXT NOT NULL CHECK(state IN ('submitted','uncertain','deferred','failed','unavailable','stale')),
    outcome_json TEXT NOT NULL CHECK(json_valid(outcome_json)),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (attempt_id, evidence_hash)
) STRICT""",
    """CREATE TRIGGER inbox_nudge_needs_identity BEFORE UPDATE ON inbox_nudge_needs
WHEN NEW.agent_id IS NOT OLD.agent_id OR NEW.opened_at_first IS NOT OLD.opened_at_first
  OR NEW.cycle < OLD.cycle OR NEW.next_sequence < OLD.next_sequence
BEGIN
    SELECT RAISE(ABORT, 'A nudge need keeps its agent, its first opening, and its cycle and attempt counters only move forward');
END""",
    """CREATE TRIGGER inbox_nudge_needs_no_delete BEFORE DELETE ON inbox_nudge_needs
BEGIN
    SELECT RAISE(ABORT, 'Nudge needs are never deleted');
END""",
    """CREATE TRIGGER inbox_nudge_attempts_identity BEFORE UPDATE ON inbox_nudge_attempts
WHEN NEW.id IS NOT OLD.id OR NEW.agent_id IS NOT OLD.agent_id OR NEW.cycle IS NOT OLD.cycle
  OR NEW.sequence IS NOT OLD.sequence OR NEW.kind IS NOT OLD.kind OR NEW.binding_json IS NOT OLD.binding_json
  OR NEW.session_key IS NOT OLD.session_key OR NEW.event_ids_json IS NOT OLD.event_ids_json
  OR NEW.progress_json IS NOT OLD.progress_json OR NEW.prepared_at IS NOT OLD.prepared_at
  OR (OLD.state <> 'prepared' AND OLD.state <> 'uncertain' AND NEW.state IS NOT OLD.state)
  OR (OLD.state = 'uncertain' AND NEW.state NOT IN ('uncertain','submitted'))
  OR (OLD.state <> 'prepared' AND NEW.state = 'prepared')
BEGIN
    SELECT RAISE(ABORT, 'A nudge attempt keeps its identity and exact binding; only unsettled uncertainty may later be reconciled');
END""",
    """CREATE TRIGGER inbox_nudge_attempts_no_delete BEFORE DELETE ON inbox_nudge_attempts
BEGIN
    SELECT RAISE(ABORT, 'Nudge attempts are never deleted');
END""",
    """CREATE TRIGGER inbox_nudge_evidence_no_update BEFORE UPDATE ON inbox_nudge_evidence
BEGIN
    SELECT RAISE(ABORT, 'Nudge evidence is append-only');
END""",
    """CREATE TRIGGER inbox_nudge_evidence_no_delete BEFORE DELETE ON inbox_nudge_evidence
BEGIN
    SELECT RAISE(ABORT, 'Nudge evidence is append-only');
END""",
)


def storage_contribution() -> StorageContribution:
    """The engine's storage requirements, for the host's assembly. The host owns ``inbox_events`` and ``inbox_deliveries``."""
    return StorageContribution(OWNER, TABLE_SPECS, IMMUTABLE_FIELDS, SCHEMA, required_tables=REQUIRED_TABLES)
