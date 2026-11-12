"""Target DDL for the tables the operations engine owns.

This is the maintained v1.3 target schema for isolated empty instances: it is edited directly, not generated from
a migration chain. Nothing here upgrades an existing database (``Database`` refuses a store whose identity or schema
differs and performs no conversion), and no historical migration chain is carried. Retained domains contribute their
own tables, indexes, triggers and sequence seeds.
"""

CORE_SCHEMA = (
    """CREATE TABLE "principals" (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('human','agent','system','integration')),
    display_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE businesses (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL UNIQUE,
    hq_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    config_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, archived_at TEXT, archived_by TEXT REFERENCES principals(id))""",
    """CREATE TABLE business_memberships (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    is_owner INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, person_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(person_json)),
    PRIMARY KEY (business_id, principal_id)
)""",
    """CREATE TABLE installation (
    id TEXT PRIMARY KEY CHECK (id = 'service'),
    installation_id TEXT NOT NULL UNIQUE CHECK (
        length(installation_id) = 36 AND lower(installation_id) = installation_id
        AND substr(installation_id, 9, 1) = '-' AND substr(installation_id, 14, 1) = '-'
        AND substr(installation_id, 19, 1) = '-' AND substr(installation_id, 24, 1) = '-'
        AND length(replace(installation_id, '-', '')) = 32
        AND replace(installation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    owner_principal_id TEXT NOT NULL REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, management_coordinator_id TEXT REFERENCES principals(id))""",
    """CREATE TABLE agent_profiles (
    agent_id TEXT PRIMARY KEY REFERENCES principals(id),
    principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id),
    home_business_id TEXT REFERENCES businesses(id),
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    workspace TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','retired')),
    profile_json TEXT NOT NULL CHECK (json_valid(profile_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (agent_id = principal_id),
    CHECK ((home_business_id IS NULL AND namespace = 'ecosystem') OR namespace = home_business_id)
)""",
    """CREATE UNIQUE INDEX agent_profile_active_names ON agent_profiles(namespace, name) WHERE status = 'active'""",
    """CREATE UNIQUE INDEX agent_profiles_one_active_role_per_home ON agent_profiles(home_business_id, json_extract(profile_json, '$.role.id'))
WHERE status = 'active' AND home_business_id IS NOT NULL""",
    """CREATE TABLE "signoffs" (
    id TEXT PRIMARY KEY,
    review_business_id TEXT REFERENCES businesses(id),
    review_scope_kind TEXT NOT NULL DEFAULT 'business' CHECK (review_scope_kind IN ('business','global')),
    proposer_principal_id TEXT NOT NULL REFERENCES principals(id),
    proposer_agent_id TEXT REFERENCES agent_profiles(agent_id),
    initiated_by_principal_id TEXT REFERENCES principals(id),
    operation_name TEXT NOT NULL,
    operation_version INTEGER NOT NULL,
    proposal_revision INTEGER NOT NULL CHECK (proposal_revision >= 1),
    payload_hash TEXT NOT NULL,
    change_set BLOB NOT NULL,
    preview_json TEXT NOT NULL CHECK (json_valid(preview_json)),
    expected_targets_json TEXT NOT NULL CHECK (json_valid(expected_targets_json)),
    policy_binding_json TEXT NOT NULL CHECK (json_valid(policy_binding_json)),
    affected_scope_json TEXT NOT NULL CHECK (json_valid(affected_scope_json)),
    state TEXT NOT NULL CHECK (state IN ('pending','approved','applied','rejected','conflicted','failed','answered','cancelled')),
    status_revision INTEGER NOT NULL DEFAULT 1 CHECK (status_revision >= 1),
    updated_sequence INTEGER NOT NULL UNIQUE,
    decision_json TEXT CHECK (decision_json IS NULL OR json_valid(decision_json)),
    applied_at TEXT,
    apply_result_json TEXT CHECK (apply_result_json IS NULL OR json_valid(apply_result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    supersedes_id TEXT UNIQUE REFERENCES signoffs(id),
    superseded_by_id TEXT UNIQUE REFERENCES signoffs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'internal_change' CHECK(category IN ('internal_change','platform_action','input_request','side_chat')),
    execution_job_id TEXT,
    execution_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(execution_json) AND json_type(execution_json)='object'),
    legacy_evidence_json TEXT CHECK(legacy_evidence_json IS NULL OR json_valid(legacy_evidence_json)),
    request_key TEXT,
    display_title TEXT CHECK(display_title IS NULL OR (category='side_chat' AND length(trim(display_title)) BETWEEN 1 AND 200)),
    CHECK ((review_scope_kind = 'business' AND review_business_id IS NOT NULL) OR (review_scope_kind = 'global' AND review_business_id IS NULL))
)""",
    """CREATE INDEX signoffs_category_queue ON signoffs(review_business_id, category, state, created_at, id)""",
    """CREATE UNIQUE INDEX signoffs_execution_job ON signoffs(execution_job_id) WHERE execution_job_id IS NOT NULL""",
    """CREATE UNIQUE INDEX signoffs_open_request_key ON signoffs(proposer_principal_id, COALESCE(review_business_id, ''), request_key)
WHERE request_key IS NOT NULL AND state='pending' AND superseded_by_id IS NULL""",
    """CREATE INDEX signoffs_queue ON signoffs(review_scope_kind, review_business_id, state, created_at, id)""",
    """CREATE INDEX signoffs_updates ON signoffs(review_scope_kind, review_business_id, updated_sequence)""",
    """CREATE INDEX signoffs_working_attention ON signoffs(proposer_principal_id,review_business_id,updated_sequence DESC)""",
    """CREATE TRIGGER signoffs_immutable_payload
BEFORE UPDATE OF id, review_scope_kind, review_business_id, proposer_principal_id, proposer_agent_id,
    initiated_by_principal_id, operation_name, operation_version, proposal_revision,
    payload_hash, change_set, preview_json, expected_targets_json, policy_binding_json,
    affected_scope_json, supersedes_id, created_at ON signoffs
BEGIN
    SELECT RAISE(ABORT, 'signoff proposal bytes and identity are immutable');
END""",
    """CREATE TRIGGER signoffs_immutable_platform_binding
BEFORE UPDATE OF category, execution_job_id, legacy_evidence_json ON signoffs
BEGIN
    SELECT RAISE(ABORT, 'signoff category, execution identity and historical evidence are immutable');
END""",
    """CREATE TRIGGER signoffs_immutable_request_key BEFORE UPDATE OF request_key ON signoffs
BEGIN SELECT RAISE(ABORT, 'signoff request key is immutable'); END""",
    """CREATE TRIGGER signoffs_immutable_response BEFORE UPDATE OF decision_json ON signoffs
WHEN OLD.decision_json IS NOT NULL AND NEW.decision_json IS NOT OLD.decision_json
BEGIN SELECT RAISE(ABORT, 'submitted owner response is immutable'); END""",
    """CREATE TABLE signoff_notes (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 signoff_id TEXT NOT NULL REFERENCES signoffs(id),
 body TEXT NOT NULL CHECK(length(trim(body))>0 AND length(body)<=20000),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(refs_json))
, summary TEXT)""",
    """CREATE INDEX signoff_notes_thread ON signoff_notes(signoff_id,sequence)""",
    """CREATE TRIGGER signoff_notes_no_delete BEFORE DELETE ON signoff_notes BEGIN SELECT RAISE(ABORT,'Notes are immutable'); END""",
    """CREATE TRIGGER signoff_notes_no_update BEFORE UPDATE ON signoff_notes BEGIN SELECT RAISE(ABORT,'Notes are immutable'); END""",
    """CREATE TABLE signoff_attention (
    signoff_id TEXT PRIMARY KEY REFERENCES signoffs(id),
    notify INTEGER NOT NULL DEFAULT 0 CHECK(notify IN (0,1)),
    urgent INTEGER NOT NULL DEFAULT 0 CHECK(urgent IN (0,1)),
    blocking INTEGER NOT NULL DEFAULT 0 CHECK(blocking IN (0,1)),
    reason TEXT NOT NULL DEFAULT '',
    deadline TEXT,
    next_notify_at TEXT,
    snoozed_until TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
, priority_updated_by TEXT REFERENCES principals(id), priority_updated_at TEXT)""",
    """CREATE TABLE signoff_notifications (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES principals(id),
    delivery_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('individual','summary')),
    signoff_ids_json TEXT NOT NULL CHECK(json_valid(signoff_ids_json)),
    created_at TEXT NOT NULL,
    dismissed_at TEXT
)""",
    """CREATE INDEX signoff_notifications_owner ON signoff_notifications(owner_id, dismissed_at, created_at)""",
    """CREATE TABLE signoff_notification_schedule (
    owner_id TEXT PRIMARY KEY REFERENCES principals(id),
    last_summary_date TEXT NOT NULL
)""",
    """CREATE TABLE idempotency_receipts (
    principal_id TEXT NOT NULL REFERENCES principals(id),
    target_namespace TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, target_namespace, operation_name, idempotency_key)
)""",
    """CREATE TRIGGER idempotency_receipts_no_update BEFORE UPDATE ON idempotency_receipts
BEGIN
    SELECT RAISE(ABORT, 'idempotency receipts are immutable');
END""",
    """CREATE TABLE apply_receipts (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    target_namespace TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    change_set BLOB NOT NULL,
    signoff_id TEXT UNIQUE REFERENCES signoffs(id),
    state TEXT NOT NULL CHECK (state IN ('prepared','applying','applied','failed','conflicted')),
    progress_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(progress_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TRIGGER apply_receipts_immutable_payload
BEFORE UPDATE OF id, principal_id, target_namespace, operation_name, payload_hash,
    change_set, signoff_id, created_at ON apply_receipts
BEGIN
    SELECT RAISE(ABORT, 'application receipt bindings are immutable');
END""",
    """CREATE TABLE resource_claims (
    target_namespace TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    apply_id TEXT NOT NULL REFERENCES apply_receipts(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (target_namespace, resource_type, resource_id)
)""",
    """CREATE TABLE maintenance_state (
    id TEXT PRIMARY KEY CHECK (id = 'service'),
    state TEXT NOT NULL CHECK (state IN ('open','draining','closed','retired')),
    message TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE change_sequences (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0 CHECK (value >= 0)
)""",
    """CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    target_namespace TEXT NOT NULL,
    kind TEXT NOT NULL,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    initiated_by_principal_id TEXT REFERENCES principals(id),
    arguments_json TEXT NOT NULL CHECK (json_valid(arguments_json)),
    definition_ref_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(definition_ref_json)),
    dedupe_key TEXT NOT NULL,
    state TEXT NOT NULL,
    claimed_by TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    logs_path TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (target_namespace, kind, dedupe_key)
)""",
    """CREATE INDEX jobs_business_kind_created ON jobs(business_id, kind, created_at)""",
    """CREATE INDEX jobs_ready ON jobs(state, next_retry_at, created_at, id)""",
    """CREATE TRIGGER jobs_immutable_bindings BEFORE UPDATE ON jobs
WHEN NEW.id IS NOT OLD.id OR NEW.business_id IS NOT OLD.business_id
 OR NEW.target_namespace IS NOT OLD.target_namespace OR NEW.kind IS NOT OLD.kind
 OR NEW.principal_id IS NOT OLD.principal_id OR NEW.initiated_by_principal_id IS NOT OLD.initiated_by_principal_id
 OR NEW.arguments_json IS NOT OLD.arguments_json OR NEW.definition_ref_json IS NOT OLD.definition_ref_json
 OR NEW.dedupe_key IS NOT OLD.dedupe_key OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'immutable job binding'); END""",
    """CREATE TRIGGER jobs_immutable_dispatch_intent BEFORE UPDATE OF result_json ON jobs
WHEN json_type(OLD.result_json, '$.dispatch_intent') IS NOT NULL
 AND json_type(OLD.result_json, '$.dispatch_intent') != 'null'
 AND json_extract(NEW.result_json, '$.dispatch_intent') IS NOT json_extract(OLD.result_json, '$.dispatch_intent')
BEGIN SELECT RAISE(ABORT, 'immutable dispatch intent'); END""",
    """CREATE TRIGGER jobs_immutable_external_receipt BEFORE UPDATE OF result_json ON jobs
WHEN json_type(OLD.result_json, '$.external_receipt') IS NOT NULL
 AND json_type(OLD.result_json, '$.external_receipt') != 'null'
 AND json_extract(NEW.result_json, '$.external_receipt') IS NOT json_extract(OLD.result_json, '$.external_receipt')
BEGIN SELECT RAISE(ABORT, 'immutable external receipt'); END""",
    """CREATE TRIGGER jobs_immutable_outputs BEFORE UPDATE OF result_json ON jobs
WHEN json_type(OLD.result_json, '$.outputs') IS NOT NULL
 AND (json_extract(NEW.result_json, '$.outputs') IS NOT json_extract(OLD.result_json, '$.outputs')
      OR json_extract(NEW.result_json, '$.outputs_hash') IS NOT json_extract(OLD.result_json, '$.outputs_hash'))
BEGIN SELECT RAISE(ABORT, 'immutable job outputs'); END""",
    """CREATE TABLE routines (
    namespace TEXT NOT NULL,
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('business','global')),
    target TEXT NOT NULL,
    schedule TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','paused')),
    job_kind TEXT NOT NULL,
    arguments_json TEXT NOT NULL CHECK (json_valid(arguments_json)),
    timeout_seconds INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, id),
    CHECK ((scope = 'business' AND business_id IS NOT NULL AND namespace = business_id)
        OR (scope = 'global' AND business_id IS NULL))
)""",
    """CREATE TABLE routine_runs (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    routine_id TEXT NOT NULL,
    business_id TEXT REFERENCES businesses(id),
    trigger TEXT NOT NULL,
    slot TEXT,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_json)),
    cost_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(cost_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (namespace, routine_id) REFERENCES routines(namespace, id)
)""",
    """CREATE INDEX routine_runs_business_started ON routine_runs(business_id, started_at)""",
    """CREATE INDEX routine_runs_namespace_started ON routine_runs(namespace, started_at)""",
    """CREATE INDEX routine_runs_state_started ON routine_runs(state, started_at)""",
    """CREATE TABLE routine_slots (
    namespace TEXT NOT NULL,
    routine_id TEXT NOT NULL,
    target_namespace TEXT NOT NULL,
    schedule_revision INTEGER NOT NULL,
    slot TEXT NOT NULL,
    wall_slot TEXT NOT NULL,
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace, routine_id, target_namespace),
    FOREIGN KEY (namespace, routine_id) REFERENCES routines(namespace, id)
)""",
    """CREATE TABLE "relationships" (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    from_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_type TEXT NOT NULL,
    to_id TEXT NOT NULL,
    role TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT REFERENCES principals(id),
    CHECK (business_id IS NOT NULL OR (to_type='task' AND role='concerns') OR (from_type='task' AND role='concerns')
        OR (from_type='task' AND ((role='assigned_to' AND to_type IN ('agent','person')) OR (role='blocked_by' AND to_type='task')))
        OR (from_type IN ('material','topic','person') AND (
            (role='part_of' AND to_type='topic')
            OR (role IN ('concerns','mentions') AND to_type IN ('topic','person'))
            OR (role='extracted_from' AND to_type='material')))),
    PRIMARY KEY (business_id, id)
)""",
    """CREATE INDEX relationships_from ON relationships(business_id,from_type,from_id,role)""",
    """CREATE UNIQUE INDEX relationships_global_key ON relationships(id) WHERE business_id IS NULL""",
    """CREATE INDEX relationships_memory_observe ON relationships(business_id,from_type,from_id,id)""",
    """CREATE INDEX relationships_role ON relationships(business_id,role,updated_at,id)""",
    """CREATE INDEX relationships_to ON relationships(business_id,to_type,to_id,role)""",
    """CREATE TABLE context_reviews (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    id TEXT NOT NULL,
    relationship_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_token TEXT NOT NULL,
    relationship_revision INTEGER NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('incorporated','no_change','pending')),
    explanation TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES principals(id),
    prepared_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(business_id,id)
)""",
    """CREATE INDEX context_reviews_history ON context_reviews(business_id,item_type,item_id,relationship_id,created_at,id)""",
    """CREATE TRIGGER context_reviews_no_delete BEFORE DELETE ON context_reviews BEGIN
    SELECT RAISE(ABORT,'Context review decisions are immutable');
END""",
    """CREATE TRIGGER context_reviews_no_update BEFORE UPDATE ON context_reviews BEGIN
    SELECT RAISE(ABORT,'Context review decisions are immutable');
END""",
    """CREATE TABLE context_review_states (
    business_id TEXT NOT NULL,
    relationship_id TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(business_id,relationship_id,item_type,item_id),
    FOREIGN KEY(business_id,relationship_id) REFERENCES relationships(business_id,id) ON DELETE CASCADE,
    FOREIGN KEY(business_id,review_id) REFERENCES context_reviews(business_id,id)
)""",
    """INSERT INTO change_sequences(name, value) VALUES ('signoffs', 0)""",
)
