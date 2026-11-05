"""The Context model-call usage ledger: two tables composed into the operational schema through one storage contribution.

``context_usage_calls`` keeps one durable row per stable library ``call_id``. A row is bound immutably to the stored queue
input, the Context event that input reserved, the library attempt (its ID, its own 1-based ``library_attempt_sequence`` and
the stage). The owning namespace is the input's (``business_id`` NULL is Global) and is never supplied by a caller. The
library attempt sequence is deliberately not the queue attempt number; nothing here relates the two.

``context_usage_sync`` keeps what the later host collector last observed about one queue input and Context event:
``partial`` (an intermediate page), ``ready`` (a bounded traversal reached EOF) or ``unavailable`` (unreadable or corrupt
evidence). It never proves that no call exists, and recording it never deletes an imported call.

Unknown is NULL. Cost is a canonical decimal text so it sums exactly. No prompt, source, tool payload or credential column
exists. Rows are retained: calls and sync records are never deleted, a terminal call is final, and the identity columns
cannot change.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

STAGES = ("bootstrap", "propagation")
STATES = ("started", "completed", "failed")
SYNC_STATUSES = ("partial", "ready", "unavailable")
SCOPES = ("native", "call")
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")

_EVENT = "length({c}) = 5 AND {c} NOT GLOB '*[^a-z0-9]*'"
_TIME = ("length({c}) = 27 AND {c} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]."
         "[0-9][0-9][0-9][0-9][0-9][0-9]Z'")
_BOOTSTRAP = "[0-9a-f]" * 32 + ":bootstrap"
_COUNTERS = ",\n    ".join(
    f"{name} INTEGER CHECK ({name} IS NULL OR (typeof({name}) = 'integer' AND {name} >= 0))" for name in TOKEN_FIELDS)

SCHEMA = (
    f"""CREATE TABLE context_usage_calls (
    call_id TEXT PRIMARY KEY CHECK (call_id = attempt_id || ':' || stage),
    business_id TEXT REFERENCES businesses(id),
    input_id TEXT NOT NULL REFERENCES context_inputs(id),
    context_event_id TEXT NOT NULL CHECK ({_EVENT.format(c='context_event_id')}),
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) = 32 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
    library_attempt_sequence INTEGER NOT NULL CHECK (typeof(library_attempt_sequence) = 'integer' AND library_attempt_sequence >= 1),
    stage TEXT NOT NULL CHECK (stage IN ('bootstrap','propagation')),
    state TEXT NOT NULL CHECK (state IN ('started','completed','failed')),
    started_at TEXT NOT NULL CHECK ({_TIME.format(c='started_at')}),
    finished_at TEXT CHECK (finished_at IS NULL OR ({_TIME.format(c='finished_at')})),
    configured_model TEXT NOT NULL CHECK (length(configured_model) BETWEEN 1 AND 200),
    configured_effort TEXT NOT NULL CHECK (length(configured_effort) BETWEEN 1 AND 200),
    configured_service_tier TEXT NOT NULL CHECK (length(configured_service_tier) BETWEEN 1 AND 200),
    observed_provider TEXT CHECK (observed_provider IS NULL OR length(observed_provider) BETWEEN 1 AND 200),
    observed_model TEXT CHECK (observed_model IS NULL OR length(observed_model) BETWEEN 1 AND 200),
    native_thread_id TEXT CHECK (native_thread_id IS NULL OR length(native_thread_id) BETWEEN 1 AND 200),
    native_turn_id TEXT CHECK (native_turn_id IS NULL OR length(native_turn_id) BETWEEN 1 AND 200),
    {_COUNTERS},
    cost_usd TEXT CHECK (cost_usd IS NULL OR (typeof(cost_usd) = 'text' AND length(cost_usd) BETWEEN 1 AND 40
        AND cost_usd GLOB '[0-9]*' AND cost_usd NOT GLOB '*[^0-9.]*')),
    usage_source TEXT CHECK (usage_source IS NULL OR length(usage_source) BETWEEN 1 AND 64),
    usage_scope TEXT CHECK (usage_scope IS NULL OR usage_scope IN ('native','call')),
    usage_updates INTEGER NOT NULL CHECK (typeof(usage_updates) = 'integer' AND usage_updates >= 0),
    ignored_updates INTEGER NOT NULL CHECK (typeof(ignored_updates) = 'integer' AND ignored_updates >= 0),
    bootstrap_call_id TEXT CHECK (bootstrap_call_id IS NULL OR (stage = 'propagation' AND bootstrap_call_id GLOB '{_BOOTSTRAP}')),
    error_type TEXT CHECK (error_type IS NULL OR length(error_type) BETWEEN 1 AND 80),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 300),
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((state = 'started') = (finished_at IS NULL)),
    CHECK ((state = 'failed') = (error_type IS NOT NULL)),
    CHECK ((error_type IS NULL) = (error_message IS NULL)),
    CHECK ((usage_updates > 0) = (usage_source IS NOT NULL) AND (usage_source IS NOT NULL) = (usage_scope IS NOT NULL)),
    CHECK (usage_scope IS NOT 'native' OR (native_thread_id IS NOT NULL AND native_turn_id IS NOT NULL))
)""",
    """CREATE INDEX context_usage_calls_by_input ON context_usage_calls(input_id, started_at, call_id)""",
    """CREATE INDEX context_usage_calls_by_start ON context_usage_calls(started_at, call_id)""",
    """CREATE INDEX context_usage_calls_by_bootstrap ON context_usage_calls(bootstrap_call_id)""",
    """CREATE TRIGGER context_usage_calls_binding
BEFORE INSERT ON context_usage_calls
WHEN NEW.business_id IS NOT (SELECT business_id FROM context_inputs WHERE id = NEW.input_id)
    OR NOT EXISTS (SELECT 1 FROM context_input_attempts a
                   WHERE a.input_id = NEW.input_id AND a.context_event_id = NEW.context_event_id)
BEGIN
    SELECT RAISE(ABORT, 'A usage call belongs to its queue input''s namespace and a Context event that input reserved');
END""",
    """CREATE TRIGGER context_usage_calls_bootstrap_link
BEFORE INSERT ON context_usage_calls
WHEN EXISTS (
    SELECT 1 FROM context_usage_calls other
    WHERE ((NEW.bootstrap_call_id IS NOT NULL AND other.call_id = NEW.bootstrap_call_id)
        OR (NEW.bootstrap_call_id IS NOT NULL AND other.bootstrap_call_id = NEW.bootstrap_call_id)
        OR (NEW.stage = 'bootstrap' AND other.bootstrap_call_id = NEW.call_id))
      AND (other.input_id <> NEW.input_id OR other.context_event_id <> NEW.context_event_id))
BEGIN
    SELECT RAISE(ABORT, 'A bootstrap call and every call that references it belong to one input and Context event');
END""",
    """CREATE TRIGGER context_usage_calls_immutable_identity
BEFORE UPDATE OF call_id, business_id, input_id, context_event_id, attempt_id, library_attempt_sequence, stage, started_at,
    configured_model, configured_effort, configured_service_tier, bootstrap_call_id, recorded_at
ON context_usage_calls
BEGIN
    SELECT RAISE(ABORT, 'A usage call identity is immutable');
END""",
    """CREATE TRIGGER context_usage_calls_final
BEFORE UPDATE ON context_usage_calls
WHEN OLD.state IN ('completed','failed')
BEGIN
    SELECT RAISE(ABORT, 'A completed or failed usage call is final');
END""",
    """CREATE TRIGGER context_usage_calls_native_identity
BEFORE UPDATE OF native_thread_id, native_turn_id ON context_usage_calls
WHEN (OLD.native_thread_id IS NOT NULL AND NEW.native_thread_id IS NOT OLD.native_thread_id)
  OR (OLD.native_turn_id IS NOT NULL AND NEW.native_turn_id IS NOT OLD.native_turn_id)
BEGIN
    SELECT RAISE(ABORT, 'A native thread or turn identity cannot be reassigned');
END""",
    """CREATE TRIGGER context_usage_calls_no_rollback
BEFORE UPDATE ON context_usage_calls
WHEN NEW.usage_updates < OLD.usage_updates OR NEW.ignored_updates < OLD.ignored_updates
BEGIN
    SELECT RAISE(ABORT, 'A newer usage observation cannot be rolled back');
END""",
    """CREATE TRIGGER context_usage_calls_keep_history
BEFORE DELETE ON context_usage_calls
BEGIN
    SELECT RAISE(ABORT, 'Usage call history is retained');
END""",
    f"""CREATE TABLE context_usage_sync (
    input_id TEXT NOT NULL REFERENCES context_inputs(id),
    context_event_id TEXT NOT NULL CHECK ({_EVENT.format(c='context_event_id')}),
    business_id TEXT REFERENCES businesses(id),
    status TEXT NOT NULL CHECK (status IN ('partial','ready','unavailable')),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    error_message TEXT CHECK (error_message IS NULL OR length(error_message) <= 300),
    checked_at TEXT NOT NULL CHECK ({_TIME.format(c='checked_at')}),
    PRIMARY KEY (input_id, context_event_id),
    CHECK ((error_code IS NULL) = (error_message IS NULL)),
    CHECK (status != 'ready' OR error_code IS NULL),
    CHECK (status != 'unavailable' OR error_code IS NOT NULL)
)""",
    """CREATE TRIGGER context_usage_sync_binding
BEFORE INSERT ON context_usage_sync
WHEN NEW.business_id IS NOT (SELECT business_id FROM context_inputs WHERE id = NEW.input_id)
    OR NOT EXISTS (SELECT 1 FROM context_input_attempts a
                   WHERE a.input_id = NEW.input_id AND a.context_event_id = NEW.context_event_id)
BEGIN
    SELECT RAISE(ABORT, 'A usage sync record belongs to its queue input''s namespace and a Context event that input reserved');
END""",
    """CREATE TRIGGER context_usage_sync_immutable_identity
BEFORE UPDATE OF input_id, context_event_id, business_id ON context_usage_sync
BEGIN
    SELECT RAISE(ABORT, 'A usage sync record identity is immutable');
END""",
    """CREATE TRIGGER context_usage_sync_keep_history
BEFORE DELETE ON context_usage_sync
BEGIN
    SELECT RAISE(ABORT, 'Usage sync history is retained');
END""",
)

TABLES = {
    "context_usage_calls": TableSpec(("call_id",), "business_id", True, None),
    "context_usage_sync": TableSpec(("input_id", "context_event_id"), "business_id", True, None),
}
IMMUTABLE_FIELDS = {
    "context_usage_calls": frozenset({
        "call_id", "business_id", "input_id", "context_event_id", "attempt_id", "library_attempt_sequence", "stage",
        "started_at", "configured_model", "configured_effort", "configured_service_tier", "bootstrap_call_id", "recorded_at"}),
    "context_usage_sync": frozenset({"input_id", "context_event_id", "business_id"}),
}
# Calls are bound to the stored queue input and to one of its attempts; the queue contribution owns both tables.
REQUIRED_TABLES = frozenset({"context_inputs", "context_input_attempts"})


def storage_contribution() -> StorageContribution:
    return StorageContribution("context-usage", TABLES, IMMUTABLE_FIELDS, SCHEMA, required_tables=REQUIRED_TABLES)
