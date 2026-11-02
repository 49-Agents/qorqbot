"""Telegram-owned transport storage; selected only with the Telegram extension."""
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

TABLE_SPECS = {
    'telegram_state': TableSpec(("id",)),
    'telegram_updates': TableSpec(("bot_id", "update_id")),
    'telegram_messages': TableSpec(("id",)),
    'telegram_outbox': TableSpec(("id",)),
    'telegram_events': TableSpec(("producer_id", "event_key")),
    'telegram_signoff_posts': TableSpec(("id",)),
    'telegram_owner_inputs': TableSpec(("id",)),
    'telegram_cleanup_jobs': TableSpec(("id",)),
}

IMMUTABLE_FIELDS = {
    'telegram_state': frozenset({"bot_id", "installation_id", "activation_at"}),
    'telegram_updates': frozenset({"kind", "received_at"}),
    'telegram_messages': frozenset({"direction", "chat_name", "chat_id", "message_thread_id", "agent_id", "purpose", "occurred_at", "request_key", "input_mode"}),
    'telegram_outbox': frozenset({"chat_name", "chat_id", "message_thread_id", "kind", "source_kind", "source_key", "part", "message_id", "payload_json", "event_producer", "event_key", "event_sequence"}),
    'telegram_signoff_posts': frozenset({"signoff_id", "generation", "category", "proposal_revision", "payload_hash", "expected_status_revision", "input_form"}),
    'telegram_owner_inputs': frozenset({"purpose", "correlation_id", "message_id", "agent_id", "sensitive", "owner_principal_id", "config_generation", "binding_fingerprint", "target_fingerprint"}),
    'telegram_cleanup_jobs': frozenset({"message_id"}),
}

SCHEMA = (
        """CREATE TABLE telegram_state (
    id TEXT PRIMARY KEY CHECK(id = 'telegram'),
    bot_id INTEGER NOT NULL CHECK(bot_id > 0),
    installation_id TEXT NOT NULL CHECK(length(installation_id) = 36),
    owner_principal_id TEXT NOT NULL REFERENCES principals(id),
    owner_telegram_user_id INTEGER NOT NULL CHECK(owner_telegram_user_id > 0),
    config_generation INTEGER NOT NULL CHECK(config_generation >= 1),
    binding_fingerprint TEXT NOT NULL CHECK(length(binding_fingerprint) = 64),
    binding_effective_at TEXT NOT NULL,
    activation_at TEXT NOT NULL,
    poll_offset INTEGER NOT NULL DEFAULT 0 CHECK(poll_offset >= 0),
    last_poll_at TEXT,
    last_update_received_at TEXT,
    last_error_json TEXT CHECK(last_error_json IS NULL OR json_valid(last_error_json)),
    outage_json TEXT CHECK(outage_json IS NULL OR json_valid(outage_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT""",
        """CREATE TRIGGER telegram_state_identity BEFORE UPDATE ON telegram_state
WHEN NEW.id IS NOT OLD.id OR NEW.bot_id IS NOT OLD.bot_id OR NEW.installation_id IS NOT OLD.installation_id
  OR NEW.activation_at IS NOT OLD.activation_at
  OR NEW.created_at IS NOT OLD.created_at OR NEW.poll_offset < OLD.poll_offset
  OR NEW.config_generation < OLD.config_generation OR NEW.binding_effective_at < OLD.binding_effective_at
BEGIN
    SELECT RAISE(ABORT, 'Telegram bot and installation identity, activation cutoff, binding cutoff and poll cursor never move backwards');
END""",
        """CREATE TRIGGER telegram_state_no_delete BEFORE DELETE ON telegram_state
BEGIN
    SELECT RAISE(ABORT, 'Telegram integration state keeps its cursor and cutoff');
END""",
        """CREATE TABLE telegram_updates (
    bot_id INTEGER NOT NULL CHECK(bot_id > 0),
    update_id INTEGER NOT NULL CHECK(update_id >= 0),
    kind TEXT NOT NULL CHECK(kind IN ('message','message_reaction','ignored')),
    chat_id INTEGER,
    message_id INTEGER,
    actor_id INTEGER,
    sent_at TEXT,
    normalized_json TEXT NOT NULL CHECK(json_valid(normalized_json)),
    state TEXT NOT NULL DEFAULT 'received' CHECK(state IN ('received','done','ignored','failed')),
    reason TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    received_at TEXT NOT NULL,
    processed_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, update_id),
    CHECK(state NOT IN ('done','ignored') OR processed_at IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_updates_pending ON telegram_updates(bot_id, state, update_id)""",
        """CREATE INDEX telegram_updates_message ON telegram_updates(chat_id, message_id)""",
        """CREATE TRIGGER telegram_updates_identity BEFORE UPDATE ON telegram_updates
WHEN NEW.bot_id IS NOT OLD.bot_id OR NEW.update_id IS NOT OLD.update_id OR NEW.kind IS NOT OLD.kind
  OR (NEW.normalized_json IS NOT OLD.normalized_json AND NOT (OLD.state = 'received' AND NEW.state = 'ignored'))
  OR NEW.received_at IS NOT OLD.received_at
  OR (OLD.state IN ('done','ignored') AND NEW.state IS NOT OLD.state)
BEGIN
    SELECT RAISE(ABORT, 'A stored Telegram update keeps its normalized content (an ignored one loses only its text) and a finished update stays finished');
END""",
        """CREATE TABLE telegram_messages (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    chat_name TEXT NOT NULL CHECK(chat_name IN ('ordubashy','signoffs','urgent')),
    chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER CHECK(telegram_message_id IS NULL OR telegram_message_id > 0),
    agent_id TEXT REFERENCES principals(id),
    purpose TEXT NOT NULL CHECK(purpose IN ('reply','blocking_input','update','owner','notice','signoff_post','signoff_reply','urgent')),
    body TEXT,
    body_redacted INTEGER NOT NULL DEFAULT 0 CHECK(body_redacted IN (0,1)),
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    reply_to_id TEXT REFERENCES telegram_messages(id),
    reply_to_telegram_id INTEGER,
    request_key TEXT CHECK(request_key IS NULL OR length(request_key) BETWEEN 1 AND 200),
    input_mode TEXT CHECK(input_mode IS NULL OR input_mode IN ('text','secret','screen_action')),
    request_reason TEXT,
    request_state TEXT CHECK(request_state IS NULL OR request_state IN ('pending','answered','expired','cancelled')),
    request_expires_at TEXT,
    request_closed_at TEXT,
    update_id INTEGER,
    outbox_id TEXT,
    occurred_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, message_thread_id INTEGER CHECK(message_thread_id IS NULL OR message_thread_id > 1),
    CHECK(body_redacted = 1 OR body IS NOT NULL OR direction = 'outbound'),
    CHECK(sensitive = 0 OR body_redacted = 1),
    CHECK(sensitive = 0 OR body IS NULL),
    CHECK((request_key IS NULL) = (input_mode IS NULL) AND (request_key IS NULL) = (request_state IS NULL)),
    CHECK(purpose = 'blocking_input' OR request_key IS NULL),
    CHECK(request_state IS NULL OR request_state = 'pending' OR request_closed_at IS NOT NULL)
) STRICT""",
        """CREATE UNIQUE INDEX telegram_messages_provider ON telegram_messages(chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL""",
        """CREATE UNIQUE INDEX telegram_messages_update ON telegram_messages(update_id) WHERE update_id IS NOT NULL""",
        """CREATE INDEX telegram_messages_history ON telegram_messages(chat_name, occurred_at, id)""",
        """CREATE UNIQUE INDEX telegram_messages_open_request ON telegram_messages(chat_name, request_key)
    WHERE request_state = 'pending'""",
        """CREATE UNIQUE INDEX telegram_messages_open_secret ON telegram_messages(chat_name)
    WHERE request_state = 'pending' AND input_mode = 'secret'""",
        """CREATE TRIGGER telegram_messages_no_delete BEFORE DELETE ON telegram_messages
BEGIN
    SELECT RAISE(ABORT, 'Telegram conversation records are retained');
END""",
        """CREATE TABLE telegram_outbox (
    id TEXT PRIMARY KEY,
    chat_name TEXT NOT NULL CHECK(chat_name IN ('ordubashy','signoffs','urgent')),
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('text','document','photo','edit')),
    source_kind TEXT NOT NULL CHECK(length(source_kind) BETWEEN 1 AND 64),
    source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 300),
    part TEXT NOT NULL DEFAULT 'main' CHECK(length(part) BETWEEN 1 AND 64),
    message_id TEXT REFERENCES telegram_messages(id),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','dispatching','uncertain','sent','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    dispatch_started_at TEXT,
    provider_message_id INTEGER CHECK(provider_message_id IS NULL OR provider_message_id > 0),
    error_code TEXT,
    error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 500),
    binding_fingerprint TEXT NOT NULL CHECK(length(binding_fingerprint) = 64),
    config_generation INTEGER NOT NULL CHECK(config_generation >= 1),
    agent_id TEXT REFERENCES principals(id),
    producer_id TEXT,
    event_producer TEXT,
    event_key TEXT,
    event_sequence INTEGER CHECK(event_sequence IS NULL OR event_sequence >= 1),
    resolved_by TEXT REFERENCES principals(id),
    resolved_at TEXT,
    sent_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, message_thread_id INTEGER CHECK(message_thread_id IS NULL OR message_thread_id > 1),
    UNIQUE (source_kind, source_key, part),
    CHECK((event_producer IS NULL) = (event_key IS NULL) AND (event_key IS NULL) = (event_sequence IS NULL)),
    CHECK(state != 'sent' OR (provider_message_id IS NOT NULL AND sent_at IS NOT NULL)),
    CHECK(state NOT IN ('dispatching','uncertain') OR dispatch_started_at IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_outbox_due ON telegram_outbox(state, next_attempt_at, created_at)""",
        """CREATE INDEX telegram_outbox_chat ON telegram_outbox(chat_name, state, created_at)""",
        """CREATE INDEX telegram_outbox_message ON telegram_outbox(message_id)""",
        """CREATE INDEX telegram_outbox_event ON telegram_outbox(event_producer, event_key, event_sequence)""",
        """CREATE TRIGGER telegram_outbox_state BEFORE UPDATE OF state ON telegram_outbox
WHEN NEW.state != OLD.state AND NOT (
       (OLD.state = 'queued' AND NEW.state IN ('dispatching','cancelled','failed'))
    OR (OLD.state = 'dispatching' AND NEW.state IN ('sent','queued','failed','uncertain'))
    OR (OLD.state = 'uncertain' AND NEW.state IN ('sent','queued','failed'))
    OR (OLD.state = 'failed' AND NEW.state = 'queued'))
BEGIN
    SELECT RAISE(ABORT, 'Invalid Telegram outbox state transition');
END""",
        """CREATE TRIGGER telegram_outbox_no_delete BEFORE DELETE ON telegram_outbox
BEGIN
    SELECT RAISE(ABORT, 'Telegram delivery receipts are retained');
END""",
        """CREATE TABLE telegram_events (
    producer_id TEXT NOT NULL CHECK(length(producer_id) BETWEEN 1 AND 100),
    event_key TEXT NOT NULL CHECK(length(event_key) BETWEEN 1 AND 200),
    state TEXT NOT NULL CHECK(state IN ('firing','resolved')),
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    message_id TEXT REFERENCES telegram_messages(id),
    last_outbox_id TEXT REFERENCES telegram_outbox(id),
    occurrences INTEGER NOT NULL DEFAULT 1 CHECK(occurrences >= 1),
    first_seen_at TEXT NOT NULL,
    last_changed_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (producer_id, event_key)
) STRICT""",
        """CREATE TABLE telegram_signoff_posts (
    id TEXT PRIMARY KEY,
    signoff_id TEXT NOT NULL REFERENCES signoffs(id),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    notification_id TEXT,
    category TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL CHECK(proposal_revision >= 1),
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    expected_status_revision INTEGER NOT NULL CHECK(expected_status_revision >= 1),
    input_form TEXT NOT NULL CHECK(input_form IN ('action','binary_input','input','status')),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','active','retired')),
    retire_reason TEXT,
    message_id TEXT REFERENCES telegram_messages(id),
    outbox_id TEXT REFERENCES telegram_outbox(id),
    activation_at TEXT NOT NULL,
    refresh_at TEXT,
    retired_at TEXT,
    decision_json TEXT CHECK(decision_json IS NULL OR json_valid(decision_json)),
    handoff_json TEXT CHECK(handoff_json IS NULL OR json_valid(handoff_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (signoff_id, generation),
    CHECK((state = 'retired') = (retired_at IS NOT NULL)),
    CHECK(state != 'retired' OR retire_reason IS NOT NULL)
) STRICT""",
        """CREATE UNIQUE INDEX telegram_signoff_posts_current ON telegram_signoff_posts(signoff_id)
    WHERE state IN ('queued','active') AND input_form != 'status'""",
        """CREATE INDEX telegram_signoff_posts_message ON telegram_signoff_posts(message_id)""",
        """CREATE INDEX telegram_signoff_posts_open ON telegram_signoff_posts(state, refresh_at)""",
        """CREATE TRIGGER telegram_signoff_posts_identity BEFORE UPDATE ON telegram_signoff_posts
WHEN NEW.id IS NOT OLD.id OR NEW.signoff_id IS NOT OLD.signoff_id OR NEW.generation IS NOT OLD.generation
  OR NEW.category IS NOT OLD.category OR NEW.proposal_revision IS NOT OLD.proposal_revision
  OR NEW.payload_hash IS NOT OLD.payload_hash OR NEW.expected_status_revision IS NOT OLD.expected_status_revision
  OR NEW.input_form IS NOT OLD.input_form OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.state = 'retired' AND NEW.state IS NOT 'retired')
BEGIN
    SELECT RAISE(ABORT, 'A posted Signoff source binding is immutable and a retired post stays retired');
END""",
        """CREATE TABLE telegram_owner_inputs (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK(purpose IN ('owner_message','signoff_reply')),
    correlation_id TEXT NOT NULL CHECK(length(correlation_id) BETWEEN 1 AND 100),
    message_id TEXT NOT NULL REFERENCES telegram_messages(id),
    agent_id TEXT NOT NULL REFERENCES principals(id),
    signoff_id TEXT REFERENCES signoffs(id),
    inbox_event_id TEXT,
    -- The identity this delivery was accepted under. It is never upgraded: a delivery whose generation, owner, binding or
    -- target registration no longer matches is failed, not retargeted or reattributed.
    owner_principal_id TEXT NOT NULL REFERENCES principals(id),
    config_generation INTEGER NOT NULL CHECK(config_generation >= 1),
    binding_fingerprint TEXT NOT NULL CHECK(length(binding_fingerprint) = 64),
    target_fingerprint TEXT NOT NULL CHECK(length(target_fingerprint) = 64),
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    spool_id TEXT CHECK(spool_id IS NULL OR length(spool_id) BETWEEN 16 AND 100),
    expires_at TEXT,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','submitting','uncertain','submitted','failed','expired','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    deferral_reason TEXT,
    notice_sent_at TEXT,
    submitted_at TEXT,
    error_code TEXT,
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (correlation_id, agent_id),
    CHECK(sensitive = 0 OR (expires_at IS NOT NULL AND (spool_id IS NOT NULL OR state != 'queued'))),
    CHECK(sensitive = 1 OR spool_id IS NULL),
    CHECK(purpose != 'signoff_reply' OR signoff_id IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_owner_inputs_due ON telegram_owner_inputs(agent_id, state, created_at)""",
        """CREATE INDEX telegram_owner_inputs_message ON telegram_owner_inputs(message_id)""",
        """CREATE TRIGGER telegram_owner_inputs_identity BEFORE UPDATE ON telegram_owner_inputs
WHEN NEW.id IS NOT OLD.id OR NEW.purpose IS NOT OLD.purpose OR NEW.correlation_id IS NOT OLD.correlation_id
  OR NEW.message_id IS NOT OLD.message_id OR NEW.agent_id IS NOT OLD.agent_id OR NEW.sensitive IS NOT OLD.sensitive
  OR NEW.owner_principal_id IS NOT OLD.owner_principal_id OR NEW.config_generation IS NOT OLD.config_generation
  OR NEW.binding_fingerprint IS NOT OLD.binding_fingerprint OR NEW.target_fingerprint IS NOT OLD.target_fingerprint
  OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.state IN ('submitted','expired','cancelled') AND NEW.state IS NOT OLD.state)
  OR (OLD.state = 'uncertain' AND NEW.state NOT IN ('uncertain','failed','submitted','expired','cancelled'))
BEGIN
    SELECT RAISE(ABORT, 'Owner input identity is immutable and terminal input is never replayed');
END""",
        """CREATE TABLE telegram_cleanup_jobs (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE REFERENCES telegram_messages(id),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 100),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','deleting','deleted','failed')),
    not_before TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    error_code TEXT,
    error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 500),
    completed_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(state != 'deleted' OR completed_at IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_cleanup_due ON telegram_cleanup_jobs(state, next_attempt_at, created_at)""",
        """CREATE TRIGGER telegram_cleanup_identity BEFORE UPDATE ON telegram_cleanup_jobs
WHEN NEW.id IS NOT OLD.id OR NEW.message_id IS NOT OLD.message_id OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.state = 'deleted' AND NEW.state IS NOT 'deleted')
BEGIN
    SELECT RAISE(ABORT, 'Cleanup identity is immutable and a completed deletion stays complete');
END""",
        """CREATE INDEX telegram_messages_destination ON telegram_messages(chat_id, message_thread_id, occurred_at)""",
        """CREATE INDEX telegram_outbox_destination ON telegram_outbox(chat_id, message_thread_id, state)""",
        """CREATE TRIGGER telegram_messages_identity BEFORE UPDATE ON telegram_messages
WHEN NEW.id IS NOT OLD.id OR NEW.direction IS NOT OLD.direction OR NEW.chat_name IS NOT OLD.chat_name
  OR NEW.chat_id IS NOT OLD.chat_id OR NEW.message_thread_id IS NOT OLD.message_thread_id
  OR NEW.agent_id IS NOT OLD.agent_id OR NEW.purpose IS NOT OLD.purpose
  OR NEW.occurred_at IS NOT OLD.occurred_at OR NEW.request_key IS NOT OLD.request_key
  OR NEW.input_mode IS NOT OLD.input_mode OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.telegram_message_id IS NOT NULL AND NEW.telegram_message_id IS NOT OLD.telegram_message_id)
  OR (OLD.sensitive = 1 AND NEW.sensitive = 0)
  OR (OLD.body_redacted = 1 AND NEW.body IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'Telegram conversation identity and destination are immutable and a redacted body is never restored');
END""",
        """CREATE TRIGGER telegram_outbox_identity BEFORE UPDATE ON telegram_outbox
WHEN NEW.id IS NOT OLD.id OR NEW.chat_name IS NOT OLD.chat_name OR NEW.chat_id IS NOT OLD.chat_id
  OR NEW.message_thread_id IS NOT OLD.message_thread_id
  OR NEW.kind IS NOT OLD.kind OR NEW.source_kind IS NOT OLD.source_kind OR NEW.source_key IS NOT OLD.source_key
  OR NEW.part IS NOT OLD.part OR NEW.created_at IS NOT OLD.created_at OR NEW.payload_json IS NOT OLD.payload_json
  OR NEW.message_id IS NOT OLD.message_id OR NEW.event_producer IS NOT OLD.event_producer
  OR NEW.event_key IS NOT OLD.event_key OR NEW.event_sequence IS NOT OLD.event_sequence
BEGIN
    SELECT RAISE(ABORT, 'A saved Telegram outbox intent and its destination are immutable');
END""",
    )

def storage_contribution():
    return StorageContribution("telegram", TABLE_SPECS, IMMUTABLE_FIELDS, SCHEMA)
